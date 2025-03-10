import logging
import os
import re
import signal
from collections import defaultdict, namedtuple
from concurrent.futures import CancelledError, ProcessPoolExecutor, wait
from contextlib import contextmanager
from functools import wraps
from multiprocessing import Manager
from typing import Dict, Iterable, Mapping, Optional

from funcy import cached_property, first

from dvc.dvcfile import is_lock_file
from dvc.exceptions import DvcException
from dvc.path_info import PathInfo
from dvc.stage.run import CheckpointKilledError
from dvc.utils import relpath

from .base import (
    EXEC_BASELINE,
    EXEC_CHECKPOINT,
    EXEC_HEAD,
    EXEC_MERGE,
    EXEC_NAMESPACE,
    EXPS_NAMESPACE,
    EXPS_STASH,
    BaselineMismatchError,
    CheckpointExistsError,
    ExperimentExistsError,
    ExpRefInfo,
    MultipleBranchError,
)
from .utils import exp_refs_by_rev

logger = logging.getLogger(__name__)


def scm_locked(f):
    # Lock the experiments workspace so that we don't try to perform two
    # different sequences of git operations at once
    @wraps(f)
    def wrapper(exp, *args, **kwargs):
        with exp.scm_lock:
            return f(exp, *args, **kwargs)

    return wrapper


class Experiments:
    """Class that manages experiments in a DVC repo.

    Args:
        repo (dvc.repo.Repo): repo instance that these experiments belong to.
    """

    STASH_EXPERIMENT_FORMAT = "dvc-exp:{rev}:{baseline_rev}:{name}"
    STASH_EXPERIMENT_RE = re.compile(
        r"(?:commit: )"
        r"dvc-exp:(?P<rev>[0-9a-f]+):(?P<baseline_rev>[0-9a-f]+)"
        r":(?P<name>[^~^:\\?\[\]*]*)"
        r"(:(?P<branch>.+))?$"
    )
    BRANCH_RE = re.compile(
        r"^(?P<baseline_rev>[a-f0-9]{7})-(?P<exp_sha>[a-f0-9]+)"
        r"(?P<checkpoint>-checkpoint)?$"
    )
    LAST_CHECKPOINT = ":last"

    StashEntry = namedtuple(
        "StashEntry", ["index", "rev", "baseline_rev", "branch", "name"]
    )

    def __init__(self, repo):
        from dvc.lock import make_lock
        from dvc.scm.base import NoSCMError

        if repo.config["core"].get("no_scm", False):
            raise NoSCMError

        self.repo = repo
        self.scm_lock = make_lock(
            os.path.join(self.repo.tmp_dir, "exp_scm_lock"),
            tmp_dir=self.repo.tmp_dir,
        )

    @property
    def scm(self):
        return self.repo.scm

    @cached_property
    def dvc_dir(self):
        return relpath(self.repo.dvc_dir, self.repo.scm.root_dir)

    @contextmanager
    def chdir(self):
        yield

    @cached_property
    def args_file(self):
        from .executor import BaseExecutor

        return os.path.join(self.repo.tmp_dir, BaseExecutor.PACKED_ARGS_FILE)

    @cached_property
    def stash(self):
        from dvc.scm.git import Stash

        return Stash(self.scm, EXPS_STASH)

    @property
    def stash_revs(self):
        revs = {}
        for i, entry in enumerate(self.stash):
            msg = entry.message.decode("utf-8").strip()
            m = self.STASH_EXPERIMENT_RE.match(msg)
            if m:
                revs[entry.new_sha.decode("utf-8")] = self.StashEntry(
                    i,
                    m.group("rev"),
                    m.group("baseline_rev"),
                    m.group("branch"),
                    m.group("name"),
                )
        return revs

    def _stash_exp(
        self,
        *args,
        params: Optional[dict] = None,
        detach_rev: Optional[str] = None,
        baseline_rev: Optional[str] = None,
        branch: Optional[str] = None,
        name: Optional[str] = None,
        **kwargs,
    ):
        """Stash changes from the workspace as an experiment.

        Args:
            params: Optional dictionary of parameter values to be used.
                Values take priority over any parameters specified in the
                user's workspace.
            baseline_rev: Optional baseline rev for this experiment, defaults
                to the current SCM rev.
            branch: Optional experiment branch name. If specified, the
                experiment will be added to `branch` instead of creating
                a new branch.
            name: Optional experiment name. If specified this will be used as
                the human-readable name in the experiment branch ref. Has no
                effect of branch is specified.
        """
        with self.scm.stash_workspace(
            include_untracked=detach_rev or branch
        ) as workspace:
            # If we are not extending an existing branch, apply current
            # workspace changes to be made in new branch
            if not (branch or detach_rev) and workspace:
                self.stash.apply(workspace)
                self._prune_lockfiles()

            # checkout and detach at branch (or current HEAD)
            if detach_rev:
                head: Optional[str] = detach_rev
            elif branch:
                head = branch
            else:
                head = None

            try:
                with self.scm.detach_head(head) as rev:
                    if baseline_rev is None:
                        baseline_rev = rev

                    # update experiment params from command line
                    if params:
                        self._update_params(params)

                    # save additional repro command line arguments
                    self._pack_args(*args, **kwargs)

                    # save experiment as a stash commit
                    msg = self._stash_msg(
                        rev,
                        baseline_rev=baseline_rev,
                        branch=branch,
                        name=name,
                    )
                    stash_rev = self.stash.push(message=msg)
                    logger.debug(
                        (
                            "Stashed experiment '%s' with baseline '%s' "
                            "for future execution."
                        ),
                        stash_rev[:7],
                        baseline_rev[:7],
                    )
            finally:
                # Reset any changes before prior workspace is unstashed
                self.scm.reset(hard=True)

        return stash_rev

    def _prune_lockfiles(self):
        # NOTE: dirty DVC lock files must be restored to index state to
        # avoid checking out incorrect persist or checkpoint outs
        tree = self.scm.get_tree("HEAD")
        lock_files = [
            str(fname)
            for fname in tree.walk_files(self.scm.root_dir)
            if is_lock_file(fname)
        ]
        if lock_files:
            self.scm.reset(paths=lock_files)
            self.scm.checkout_paths(lock_files, force=True)

    def _stash_msg(
        self,
        rev: str,
        baseline_rev: str,
        branch: Optional[str] = None,
        name: Optional[str] = None,
    ):
        if not baseline_rev:
            baseline_rev = rev
        msg = self.STASH_EXPERIMENT_FORMAT.format(
            rev=rev, baseline_rev=baseline_rev, name=name if name else ""
        )
        if branch:
            return f"{msg}:{branch}"
        return msg

    def _pack_args(self, *args, **kwargs):
        import pickle

        from .executor import BaseExecutor

        if os.path.exists(self.args_file) and self.scm.is_tracked(
            self.args_file
        ):
            logger.warning(
                (
                    "Temporary DVC file '.dvc/tmp/%s' exists and was "
                    "likely committed to Git by mistake. It should be removed "
                    "with:\n"
                    "\tgit rm .dvc/tmp/%s"
                ),
                BaseExecutor.PACKED_ARGS_FILE,
                BaseExecutor.PACKED_ARGS_FILE,
            )
            with open(self.args_file, "rb") as fobj:
                try:
                    data = pickle.load(fobj)
                except Exception:  # pylint: disable=broad-except
                    data = {}
            extra = int(data.get("extra", 0)) + 1
        else:
            extra = None
        BaseExecutor.pack_repro_args(
            self.args_file, *args, extra=extra, **kwargs
        )
        self.scm.add(self.args_file)

    def _update_params(self, params: dict):
        """Update experiment params files with the specified values."""
        from benedict import benedict

        from dvc.utils.serialize import MODIFIERS

        logger.debug("Using experiment params '%s'", params)

        for params_fname in params:
            path = PathInfo(self.repo.root_dir) / params_fname
            suffix = path.suffix.lower()
            modify_data = MODIFIERS[suffix]
            with modify_data(path, tree=self.repo.tree) as data:
                benedict(data).merge(params[params_fname], overwrite=True)

        # Force params file changes to be staged in git
        # Otherwise in certain situations the changes to params file may be
        # ignored when we `git stash` them since mtime is used to determine
        # whether the file is dirty
        self.scm.add(list(params.keys()))

    def reproduce_one(self, queue=False, **kwargs):
        """Reproduce and checkout a single experiment."""
        stash_rev = self.new(**kwargs)
        if queue:
            logger.info(
                "Queued experiment '%s' for future execution.", stash_rev[:7]
            )
            return [stash_rev]
        results = self.reproduce([stash_rev], keep_stash=False)
        exp_rev = first(results)
        if exp_rev is not None:
            self._log_reproduced(results)
        return results

    def reproduce_queued(self, **kwargs):
        results = self.reproduce(**kwargs)
        if results:
            self._log_reproduced(results)
        return results

    def _log_reproduced(self, revs: Iterable[str]):
        names = []
        for rev in revs:
            name = self.get_exact_name(rev)
            names.append(name if name else rev[:7])
        fmt = (
            "\nReproduced experiment(s): %s\n"
            "To promote an experiment to a Git branch run:\n\n"
            "\tdvc exp branch <exp>\n\n"
            "To apply the results of an experiment to your workspace run:\n\n"
            "\tdvc exp apply <exp>"
        )
        logger.info(fmt, ", ".join(names))

    @scm_locked
    def new(
        self, *args, checkpoint_resume: Optional[str] = None, **kwargs,
    ):
        """Create a new experiment.

        Experiment will be reproduced and checked out into the user's
        workspace.
        """
        if checkpoint_resume is not None:
            return self._resume_checkpoint(
                *args, checkpoint_resume=checkpoint_resume, **kwargs
            )

        return self._stash_exp(*args, **kwargs)

    def _resume_checkpoint(
        self, *args, checkpoint_resume: Optional[str] = None, **kwargs,
    ):
        """Resume an existing (checkpoint) experiment.

        Experiment will be reproduced and checked out into the user's
        workspace.
        """
        assert checkpoint_resume

        if checkpoint_resume == self.LAST_CHECKPOINT:
            # Continue from most recently committed checkpoint
            resume_rev = self._get_last_checkpoint()
        else:
            resume_rev = self.scm.resolve_rev(checkpoint_resume)
        allow_multiple = "params" in kwargs
        branch: Optional[str] = self.get_branch_by_rev(
            resume_rev, allow_multiple=allow_multiple
        )
        if not branch:
            raise DvcException(
                "Could not find checkpoint experiment "
                f"'{checkpoint_resume}'"
            )

        baseline_rev = self._get_baseline(branch)
        if kwargs.get("params", None):
            logger.debug(
                "Branching from checkpoint '%s' with modified params, "
                "baseline '%s'",
                checkpoint_resume,
                baseline_rev[:7],
            )
            detach_rev = resume_rev
            branch = None
        else:
            logger.debug(
                "Continuing from tip of checkpoint '%s'", checkpoint_resume
            )
            detach_rev = None

        return self._stash_exp(
            *args,
            detach_rev=detach_rev,
            baseline_rev=baseline_rev,
            branch=branch,
            **kwargs,
        )

    def _get_last_checkpoint(self):
        rev = self.scm.get_ref(EXEC_CHECKPOINT)
        if rev:
            return rev
        raise DvcException("No existing checkpoint experiment to continue")

    @scm_locked
    def reproduce(
        self,
        revs: Optional[Iterable] = None,
        keep_stash: Optional[bool] = True,
        **kwargs,
    ) -> Mapping[str, str]:
        """Reproduce the specified experiments.

        Args:
            revs: If revs is not specified, all stashed experiments will be
                reproduced.
            keep_stash: If True, stashed experiments will be preserved if they
                fail to reproduce successfully.

        Returns:
            dict mapping successfully reproduced experiment revs to their
            hashes.
        """
        stash_revs = self.stash_revs

        # to_run contains mapping of:
        #   input_rev: (stash_index, rev, baseline_rev)
        # where input_rev contains the changes to execute (usually a stash
        # commit), rev is the original SCM commit to be checked out, and
        # baseline_rev is the experiment baseline.
        if revs is None:
            to_run = dict(stash_revs)
        else:
            to_run = {
                rev: stash_revs[rev]
                if rev in stash_revs
                else self.StashEntry(None, rev, rev, None, None)
                for rev in revs
            }

        logger.debug(
            "Reproducing experiment revs '%s'",
            ", ".join((rev[:7] for rev in to_run)),
        )

        executors = self._init_executors(to_run)
        exec_results = self._reproduce(executors, **kwargs)

        if keep_stash:
            # only drop successfully run stashed experiments
            to_drop = sorted(
                (
                    stash_revs[rev][0]
                    for rev in exec_results
                    if rev in stash_revs
                ),
                reverse=True,
            )
        else:
            # drop all stashed experiments
            to_drop = sorted(
                (stash_revs[rev][0] for rev in to_run if rev in stash_revs),
                reverse=True,
            )
        for index in to_drop:
            self.stash.drop(index)

        result: Dict[str, str] = {}
        for _, exp_result in exec_results.items():
            result.update(exp_result)
        return result

    def _init_executors(self, to_run):
        from .executor import LocalExecutor

        executors = {}
        for stash_rev, item in to_run.items():
            self.scm.set_ref(EXEC_HEAD, item.rev)
            self.scm.set_ref(EXEC_MERGE, stash_rev)
            self.scm.set_ref(EXEC_BASELINE, item.baseline_rev)

            # Executor will be initialized with an empty git repo that
            # we populate by pushing:
            #   EXEC_HEAD - the base commit for this experiment
            #   EXEC_MERGE - the unmerged changes (from our stash)
            #       to be reproduced
            #   EXEC_BASELINE - the baseline commit for this experiment
            executor = LocalExecutor(
                self.scm,
                self.dvc_dir,
                name=item.name,
                branch=item.branch,
                cache_dir=self.repo.cache.local.cache_dir,
            )
            executors[stash_rev] = executor

        for ref in (EXEC_HEAD, EXEC_MERGE, EXEC_BASELINE):
            self.scm.remove_ref(ref)

        return executors

    def _reproduce(
        self, executors: dict, jobs: Optional[int] = 1
    ) -> Mapping[str, Mapping[str, str]]:
        """Run dvc repro for the specified BaseExecutors in parallel.

        Returns:
            dict mapping stash revs to the successfully executed experiments
            for each stash rev.
        """
        result: Dict[str, Dict[str, str]] = defaultdict(dict)

        manager = Manager()
        pid_q = manager.Queue()
        with ProcessPoolExecutor(max_workers=jobs) as workers:
            futures = {}
            for rev, executor in executors.items():
                future = workers.submit(
                    executor.reproduce,
                    executor.dvc_dir,
                    pid_q,
                    rev,
                    name=executor.name,
                )
                futures[future] = (rev, executor)

            try:
                wait(futures)
            except KeyboardInterrupt:
                # forward SIGINT to any running executor processes and
                # cancel any remaining futures
                pids = {}
                while not pid_q.empty():
                    rev, pid = pid_q.get()
                    pids[rev] = pid
                for future, (rev, _) in futures.items():
                    if future.running():
                        os.kill(pids[rev], signal.SIGINT)
                    elif not future.done():
                        future.cancel()

            for future, (rev, executor) in futures.items():
                rev, executor = futures[future]
                exc = future.exception()

                try:
                    if exc is None:
                        exp_hash, force = future.result()
                        result[rev].update(
                            self._collect_executor(executor, exp_hash, force)
                        )
                    else:
                        # Checkpoint errors have already been logged
                        if not isinstance(exc, CheckpointKilledError):
                            logger.exception(
                                "Failed to reproduce experiment '%s'",
                                rev[:7],
                                exc_info=exc,
                            )
                except CancelledError:
                    logger.error(
                        "Cancelled before attempting to reproduce experiment "
                        "'%s'",
                        rev[:7],
                    )
                finally:
                    executor.cleanup()

        return result

    def _collect_executor(
        self, executor, exp_hash, force
    ) -> Mapping[str, str]:
        # NOTE: GitPython Repo instances cannot be re-used
        # after process has received SIGINT or SIGTERM, so we
        # need this hack to re-instantiate git instances after
        # checkpoint runs. See:
        # https://github.com/gitpython-developers/GitPython/issues/427
        del self.repo.scm

        results = {}

        def on_diverged(ref: str, checkpoint: bool):
            ref_info = ExpRefInfo.from_ref(ref)
            if checkpoint:
                raise CheckpointExistsError(ref_info.name)
            raise ExperimentExistsError(ref_info.name)

        for ref in executor.fetch_exps(
            self.scm, force=force, on_diverged=on_diverged,
        ):
            exp_rev = self.scm.get_ref(ref)
            if exp_rev:
                logger.debug("Collected experiment '%s'.", exp_rev[:7])
                results[exp_rev] = exp_hash

        return results

    def check_baseline(self, exp_rev):
        baseline_sha = self.repo.scm.get_rev()
        if exp_rev == baseline_sha:
            return exp_rev

        exp_baseline = self._get_baseline(exp_rev)
        if exp_baseline is None:
            # if we can't tell from branch name, fall back to parent commit
            exp_commit = self.scm.resolve_commit(exp_rev)
            if exp_commit:
                exp_baseline = first(exp_commit.parents).hexsha
        if exp_baseline == baseline_sha:
            return exp_baseline
        raise BaselineMismatchError(exp_baseline, baseline_sha)

    @scm_locked
    def get_baseline(self, rev):
        """Return the baseline rev for an experiment rev."""
        return self._get_baseline(rev)

    def _get_baseline(self, rev):
        rev = self.scm.resolve_rev(rev)

        if rev in self.stash_revs:
            entry = self.stash_revs.get(rev)
            if entry:
                return entry.baseline_rev
            return None

        ref_info = first(exp_refs_by_rev(self.scm, rev))
        if ref_info:
            return ref_info.baseline_sha
        return None

    def get_branch_by_rev(
        self, rev: str, allow_multiple: bool = False
    ) -> Optional[str]:
        """Returns full refname for the experiment branch containing rev."""
        ref_infos = list(exp_refs_by_rev(self.scm, rev))
        if not ref_infos:
            return None
        if len(ref_infos) > 1 and not allow_multiple:
            raise MultipleBranchError(rev)
        return str(ref_infos[0])

    def get_exact_name(self, rev: str):
        """Returns preferred name for the specified revision.

        Prefers tags, branches (heads), experiments in that orer.
        """
        exclude = f"{EXEC_NAMESPACE}/*"
        ref = self.scm.describe(rev, base=EXPS_NAMESPACE, exclude=exclude)
        if ref:
            return ExpRefInfo.from_ref(ref).name
        return None

    def apply(self, *args, **kwargs):
        from dvc.repo.experiments.apply import apply

        return apply(self.repo, *args, **kwargs)

    def branch(self, *args, **kwargs):
        from dvc.repo.experiments.branch import branch

        return branch(self.repo, *args, **kwargs)

    def diff(self, *args, **kwargs):
        from dvc.repo.experiments.diff import diff

        return diff(self.repo, *args, **kwargs)

    def show(self, *args, **kwargs):
        from dvc.repo.experiments.show import show

        return show(self.repo, *args, **kwargs)

    def run(self, *args, **kwargs):
        from dvc.repo.experiments.run import run

        return run(self.repo, *args, **kwargs)

    def gc(self, *args, **kwargs):
        from dvc.repo.experiments.gc import gc

        return gc(self.repo, *args, **kwargs)

    def push(self, *args, **kwargs):
        from dvc.repo.experiments.push import push

        return push(self.repo, *args, **kwargs)

    def pull(self, *args, **kwargs):
        from dvc.repo.experiments.pull import pull

        return pull(self.repo, *args, **kwargs)

    def ls(self, *args, **kwargs):
        from dvc.repo.experiments.ls import ls

        return ls(self.repo, *args, **kwargs)
