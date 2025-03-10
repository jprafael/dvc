import errno
import os

from funcy import cached_property

from dvc.tree.base import BaseTree
from dvc.utils import is_exec, relpath


class GitTree(BaseTree):  # pylint:disable=abstract-method
    """Proxies the repo file access methods to Git objects"""

    def __init__(
        self, root_dir, trie, use_dvcignore=False, dvcignore_root=None
    ):
        super().__init__(None, {})
        self._tree_root = root_dir
        self.trie = trie
        self.use_dvcignore = use_dvcignore
        self.dvcignore_root = dvcignore_root

    @property
    def rev(self):
        return self.trie.rev

    @property
    def tree_root(self):
        return self._tree_root

    def _get_key(self, path):
        if isinstance(path, str):
            if not os.path.isabs(path):
                relparts = path.split(os.sep)
            else:
                relparts = relpath(path, self.tree_root).split(os.sep)
        else:
            relparts = path.relative_to(self.tree_root).parts
        if relparts == ["."]:
            return ()
        return tuple(relparts)

    @cached_property
    def dvcignore(self):
        from dvc.ignore import DvcIgnoreFilter, DvcIgnoreFilterNoop

        root = self.dvcignore_root or self.tree_root
        cls = DvcIgnoreFilter if self.use_dvcignore else DvcIgnoreFilterNoop
        return cls(self, root)

    def open(
        self, path, mode="r", encoding=None
    ):  # pylint: disable=arguments-differ
        # NOTE: this is incorrect, we need to use locale to determine default
        # encoding.
        encoding = encoding or "utf-8"

        key = self._get_key(path)
        try:
            return self.trie.open(key, mode=mode, encoding=encoding)
        except KeyError as exc:
            msg = os.strerror(errno.ENOENT) + f"in branch '{self.rev}'"
            raise FileNotFoundError(errno.ENOENT, msg, path) from exc
        except IsADirectoryError as exc:
            raise IsADirectoryError(
                errno.EISDIR, os.strerror(errno.EISDIR), path
            ) from exc

    def exists(
        self, path, use_dvcignore=True
    ):  # pylint: disable=arguments-differ
        def _is_ignored(path):
            return self.dvcignore.is_ignored_file(
                path
            ) or self.dvcignore.is_ignored_dir(path)

        if use_dvcignore and _is_ignored(path):
            return False

        key = self._get_key(path)
        return self.trie.exists(key)

    def isdir(
        self, path, use_dvcignore=True
    ):  # pylint: disable=arguments-differ
        if use_dvcignore and self.dvcignore.is_ignored_dir(path):
            return False
        key = self._get_key(path)
        return self.trie.isdir(key)

    def isfile(self, path):  # pylint: disable=arguments-differ
        if self.dvcignore.is_ignored_file(path):
            return False
        key = self._get_key(path)
        return self.trie.isfile(key)

    def walk(
        self,
        top,
        topdown=True,
        onerror=None,
        use_dvcignore=True,
        ignore_subrepos=True,
    ):
        """Directory tree generator.

        See `os.walk` for the docs. Differences:
        - no support for symlinks
        """
        if not self.isdir(top, use_dvcignore=use_dvcignore):
            if onerror:
                if self.exists(top, use_dvcignore=use_dvcignore):
                    exc = NotADirectoryError(
                        errno.ENOTDIR, os.strerror(errno.ENOTDIR), top
                    )
                else:
                    exc = FileNotFoundError(
                        errno.ENOENT, os.strerror(errno.ENOENT), top
                    )
                onerror(exc)
            return []

        key = self._get_key(top)
        for prefix, dirs, files in self.trie.walk(key, topdown=topdown):
            if prefix:
                root = os.path.join(self.tree_root, os.sep.join(prefix))
            else:
                root = self.tree_root
            if use_dvcignore:
                dirs[:], files[:] = self.dvcignore(
                    root, dirs, files, ignore_subrepos=ignore_subrepos,
                )
            yield root, dirs, files

    def isexec(self, path):
        return is_exec(self.stat(path).st_mode)

    def stat(self, path):
        key = self._get_key(path)
        try:
            return self.trie.stat(key)
        except KeyError:
            raise FileNotFoundError(
                errno.ENOENT, os.strerror(errno.ENOENT), path
            )

    @property
    def hash_jobs(self):  # pylint: disable=invalid-overridden-method
        # NOTE: gitpython is not threadsafe. See
        # https://github.com/iterative/dvc/issues/4079
        return 1

    def walk_files(self, top):  # pylint: disable=arguments-differ
        for root, _, files in self.walk(top):
            for file in files:
                # NOTE: os.path.join is ~5.5 times slower
                yield f"{root}{os.sep}{file}"

    def _reset(self):
        return self.__dict__.pop("dvcignore", None)
