import os
from abc import ABC, abstractmethod
from typing import Callable, Iterable, Optional, Tuple

from dvc.scm.base import SCMError

from ..objects import GitObject


class NoGitBackendError(SCMError):
    def __init__(self, func):
        super().__init__(f"No valid Git backend for '{func}'")


class BaseGitBackend(ABC):
    """Base Git backend class."""

    @abstractmethod
    def __init__(self, root_dir=os.curdir, search_parent_directories=True):
        pass

    def close(self):
        pass

    @abstractmethod
    def is_ignored(self, path):
        """Return True if the specified path is gitignored."""

    @property
    @abstractmethod
    def root_dir(self) -> str:
        pass

    @staticmethod
    @abstractmethod
    def clone(
        url: str,
        to_path: str,
        rev: Optional[str] = None,
        shallow_branch: Optional[str] = None,
    ):
        pass

    @staticmethod
    @abstractmethod
    def is_sha(rev: str) -> bool:
        pass

    @property
    @abstractmethod
    def dir(self) -> str:
        pass

    @abstractmethod
    def add(self, paths: Iterable[str]):
        pass

    @abstractmethod
    def commit(self, msg: str):
        pass

    @abstractmethod
    def checkout(
        self, branch: str, create_new: Optional[bool] = False, **kwargs,
    ):
        pass

    @abstractmethod
    def pull(self, **kwargs):
        pass

    @abstractmethod
    def push(self):
        pass

    @abstractmethod
    def branch(self, branch: str):
        pass

    @abstractmethod
    def tag(self, tag: str):
        pass

    @abstractmethod
    def untracked_files(self) -> Iterable[str]:
        pass

    @abstractmethod
    def is_tracked(self, path: str) -> bool:
        pass

    @abstractmethod
    def is_dirty(self, **kwargs) -> bool:
        pass

    @abstractmethod
    def active_branch(self) -> str:
        pass

    @abstractmethod
    def list_branches(self) -> Iterable[str]:
        pass

    @abstractmethod
    def list_tags(self) -> Iterable[str]:
        pass

    @abstractmethod
    def list_all_commits(self) -> Iterable[str]:
        pass

    @abstractmethod
    def get_tree_obj(self, rev: str, **kwargs) -> GitObject:
        pass

    @abstractmethod
    def get_rev(self) -> str:
        pass

    @abstractmethod
    def resolve_rev(self, rev: str) -> str:
        pass

    @abstractmethod
    def resolve_commit(self, rev: str) -> str:
        pass

    @abstractmethod
    def branch_revs(
        self, branch: str, end_rev: Optional[str] = None
    ) -> Iterable[str]:
        pass

    @abstractmethod
    def set_ref(
        self,
        name: str,
        new_ref: str,
        old_ref: Optional[str] = None,
        message: Optional[str] = None,
        symbolic: Optional[bool] = False,
    ):
        """Set the specified git ref.

        Optional kwargs:
            old_ref: If specified, ref will only be set if it currently equals
                old_ref. Has no effect is symbolic is True.
            message: Optional reflog message.
            symbolic: If True, ref will be set as a symbolic ref to new_ref
                rather than the dereferenced object.
        """

    @abstractmethod
    def get_ref(
        self, name: str, follow: Optional[bool] = True
    ) -> Optional[str]:
        """Return the value of specified ref.

        If follow is false, symbolic refs will not be dereferenced.
        Returns None if the ref does not exist.
        """

    @abstractmethod
    def remove_ref(self, name: str, old_ref: Optional[str] = None):
        """Remove the specified git ref.

        If old_ref is specified, ref will only be removed if it currently
        equals old_ref.
        """

    @abstractmethod
    def iter_refs(self, base: Optional[str] = None):
        """Iterate over all refs in this git repo.

        If base is specified, only refs which begin with base will be yielded.
        """

    @abstractmethod
    def iter_remote_refs(self, url: str, base: Optional[str] = None):
        """Iterate over all refs in the specified remote Git repo.

        If base is specified, only refs which begin with base will be yielded.
        URL can be a named Git remote or URL.
        """

    @abstractmethod
    def get_refs_containing(self, rev: str, pattern: Optional[str] = None):
        """Iterate over all git refs containing the specfied revision."""

    @abstractmethod
    def push_refspec(
        self,
        url: str,
        src: Optional[str],
        dest: str,
        force: bool = False,
        on_diverged: Optional[Callable[[str, str], bool]] = None,
    ):
        """Push refspec to a remote Git repo.

        Args:
            url: Git remote name or absolute Git URL.
            src: Local refspec. If src ends with "/" it will be treated as a
                prefix, and all refs inside src will be pushed using dest
                as destination refspec prefix. If src is None, dest will be
                deleted from the remote.
            dest: Remote refspec.
            force: If True, remote refs will be overwritten.
            on_diverged: Callback function which will be called if local ref
                and remote have diverged and force is False. If the callback
                returns True the remote ref will be overwritten.
                Callback will be of the form:
                    on_diverged(local_refname, remote_sha)
        """

    @abstractmethod
    def fetch_refspecs(
        self,
        url: str,
        refspecs: Iterable[str],
        force: Optional[bool] = False,
        on_diverged: Optional[Callable[[str, str], bool]] = None,
    ):
        """Fetch refspecs from a remote Git repo.

        Args:
            url: Remote repo Git URL (Note this must be a Git URL and not
                a remote name).
            refspecs: Iterable containing refspecs to fetch.
                Note that this will not match subkeys.
            force: If True, local refs will be overwritten.
            on_diverged: Callback function which will be called if local ref
                and remote have diverged and force is False. If the callback
                returns True the local ref will be overwritten.
                Callback will be of the form:
                    on_diverged(local_refname, remote_sha)
        """

    @abstractmethod
    def _stash_iter(self, ref: str):
        """Iterate over stash commits in the specified ref."""

    @abstractmethod
    def _stash_push(
        self,
        ref: str,
        message: Optional[str] = None,
        include_untracked: Optional[bool] = False,
    ) -> Tuple[Optional[str], bool]:
        """Push a commit onto the specified stash."""

    @abstractmethod
    def _stash_apply(self, rev: str):
        """Apply the specified stash revision."""

    @abstractmethod
    def reflog_delete(
        self, ref: str, updateref: bool = False, rewrite: bool = False
    ):
        """Delete the specified reflog entry."""

    @abstractmethod
    def describe(
        self,
        rev: str,
        base: Optional[str] = None,
        match: Optional[str] = None,
        exclude: Optional[str] = None,
    ) -> Optional[str]:
        """Return the first ref which points to rev.

        Roughly equivalent to `git describe --all --exact-match`.

        If no matching ref was found, returns None.

        Optional kwargs:
            base: Base ref prefix to search, defaults to "refs/tags"
            match: Glob pattern. If specified only refs matching this glob
                pattern will be returned.
            exclude: Glob pattern. If specified, only refs which do not match
                this pattern will be returned.
        """

    @abstractmethod
    def diff(self, rev_a: str, rev_b: str, binary=False) -> str:
        """Return the git diff for two commits."""

    @abstractmethod
    def reset(self, hard: bool = False, paths: Iterable[str] = None):
        """Reset current git HEAD."""

    @abstractmethod
    def checkout_paths(self, paths: Iterable[str], force: bool = False):
        """Checkout the specified paths from HEAD index."""
