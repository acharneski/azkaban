#!/usr/bin/env python
# encoding: utf-8

"""Project definition module."""


from os import sep
from os.path import (abspath, basename, dirname, exists, isabs, isdir, join,
  realpath, relpath, splitext)
from traceback import format_exc
from weakref import WeakValueDictionary
from zipfile import ZipFile
from .util import AzkabanError, temppath, write_properties
import logging
import sys


logger = logging.getLogger(__name__)


class Project(object):

  """Azkaban project.

  :param name: Name of the project.
  :param register: Add project to registry. Setting this to `False` will make
    it invisible to the CLI.
  :param root: Path to a root file or directory used to enable adding files
    using relative paths (typically used with `root=__file__`).

  """

  root = None
  _registry = WeakValueDictionary()

  def __init__(self, name, root=None, register=True):
    #: Dictionary of Azkaban options which will be available to all jobs in
    #: this project. This can be used for example to set project wide defaults.
    self.properties = {}
    self.name = name
    if root:
      self.root = abspath(root if isdir(root) else dirname(root))
    if register:
      self._registry[name] = self
    self._jobs = {}
    self._files = {}

  def __str__(self):
    return self.name

  @property
  def files(self):
    """Returns a list of files that will be included in the project archive.

    This property should not be used to add files. Use :meth:`add_file`
    instead.

    """
    return self._files.values()

  @property
  def jobs(self):
    """Returns a dictionary with each job options.

    This property should not be used to add jobs. Use :meth:`add_job` instead.

    """
    return dict(
      (name, job.options)
      for name, job in self._jobs.items()
    )

  def add_file(self, path, archive_path=None, overwrite=False):
    """Include a file in the project archive.

    :param path: Path to file. If no project `root` exists, only absolute paths
      are allowed. Otherwise, this path can also be relative to said `root`.
    :param archive_path: Path to file in archive (defaults to same as `path`).
    :param overwrite: Allow overwriting any previously existing file in this
      archive path.

    If the current project has its `root` parameter specified, this method will
    allow relative paths (and join those with the project's `root`), otherwise
    it will throw an error. Furthermore, when a project `root` exists, adding
    files above it without specifying an `archive_path` will raise an error.
    This is done to avoid having files in the archive with lower level
    destinations than the base root directory.

    """
    logger.debug('adding file %r with archive path %r', path, archive_path)
    if not isabs(path):
      if not self.root:
        raise AzkabanError(
          'Relative path not allowed without specifying a project root: %r.'
          % (path, )
        )
      path = join(self.root, path)
    # disambiguate (symlinks, pardirs, etc.)
    path = realpath(path)
    if not archive_path:
      if self.root:
        if not path.startswith(self.root):
          raise AzkabanError(
            'Cannot add a file outside of the project root directory without\n'
            'specifying an archive path: %r' % (path, )
          )
        archive_path = relpath(path, self.root)
      else:
        archive_path = path
    # leading separator meaningless inside archive (trimmed automatically)
    archive_path = archive_path.lstrip('/')
    if (
      archive_path in self._files and
      self._files[archive_path] != path and
      not overwrite
    ):
      raise AzkabanError('Inconsistent duplicate file: %r.' % (path, ))
    if not exists(path):
      raise AzkabanError('File not found: %r.' % (path, ))
    self._files[archive_path] = path

  def add_job(self, name, job):
    """Include a job in the project.

    :param name: Name assigned to job (must be unique).
    :param job: :class:`~azkaban.job.Job` instance.

    This method triggers the :meth:`~azkaban.job.Job.on_add` method on the
    added job (passing the project and name as arguments). The handler will be
    called right after the job is added.

    """
    logger.debug('adding job %r', name)
    if name in self._jobs:
      raise AzkabanError('Duplicate job name: %r.' % (name, ))
    else:
      self._jobs[name] = job
      job.on_add(self, name)

  def merge_into(self, project, unregister=False):
    """Merge one project with another.

    :param project: Target :class:`Project` to merge into.
    :param unregister: Unregister project after merging it.

    The current project remains unchanged while the target project gains all
    the current project's jobs and files. Note that only project with the same
    `root` can be merged.

    """
    logger.debug('merging into project %r', project.name)
    if self.root != project.root:
      raise AzkabanError(
        'Cannot merge projects with different roots: %r and %r',
        self.root, project.root,
      )
    for name, job in self._jobs.items():
      project.add_job(name, job)
    for archive_path, path in self._files.items():
      project.add_file(path, archive_path)
    if unregister:
      self._registry.pop(self.name)

  def build(self, path, overwrite=False):
    """Create the project archive.

    :param path: Destination path.
    :param overwrite: Don't throw an error if a file already exists at `path`.

    Triggers the :meth:`~azkaban.job.Job.on_build` method on each job inside the
    project (passing itself and the job's name as two argument). This method
    will be called right before the job file is generated.

    """
    logger.debug('building project')
    # not using a with statement for compatibility with older python versions
    if exists(path) and not overwrite:
      raise AzkabanError('Path %r already exists.' % (path, ))
    if not (len(self._jobs) or len(self._files)):
      raise AzkabanError('Building empty project.')
    writer = ZipFile(path, 'w')
    try:
      if self.properties:
        with temppath() as fpath:
          write_properties(self.properties, fpath)
          writer.write(fpath, 'project.properties')
      for name, job in self._jobs.items():
        job.on_build(self, name)
        with temppath() as fpath:
          job.build(fpath)
          writer.write(fpath, '%s.job' % (name, ))
      for archive_path, path in self._files.items():
        writer.write(path, archive_path)
    finally:
      writer.close()

  @classmethod
  def load(cls, path, name=None):
    """Load project from script.

    :param path: Path to python module or package.
    :param name: Project name. If not specified and a single project is found
      while loading the script, that project is returned. In any other case
      (no/multiple projects found), an error is thrown.

    """
    sys.path.insert(0, dirname(path))
    module_name = splitext(basename(path.rstrip(sep)))[0]
    try:
      __import__(module_name)
    except ImportError:
      raise AzkabanError(
        'Unable to import script %r.\n%s' % (path, format_exc())
        )
    else:
      if name:
        try:
          return cls._registry[name]
        except KeyError:
          raise AzkabanError(
            'Unable to find project with name %r in script %r.\n'
            'Available projects: %s.'
            % (name, path, ', '.join(cls._registry))
          )
      else:
        if len(cls._registry) == 1:
          return cls._registry.popitem()[1]
        elif not cls._registry:
          raise AzkabanError('No project found in %r.' % (path, ))
        else:
          raise AzkabanError(
            'Multiple projects found in %r: %s.\n'
            'Disambiguate using --project=%s:project_name.'
            % (path, ', '.join(cls._registry), path)
          )
