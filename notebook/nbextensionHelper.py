import os
import shutil
from ipython_genutils.py3compat import cast_unicode_py2
from jupyter_core.paths import (
    jupyter_data_dir, jupyter_config_path, jupyter_path,
    SYSTEM_JUPYTER_PATH, ENV_JUPYTER_PATH,
)
from os.path import basename, join as pjoin, normpath
import tarfile
from traitlets.utils.importstring import import_item
import zipfile

from .extensions import (
    BaseExtensionApp, _get_config_dir, GREEN_ENABLED, RED_DISABLED, GREEN_OK, RED_X,
    ArgumentConflict, _base_aliases, _base_flags,
)
from ipython_genutils.tempdir import TemporaryDirectory
from urllib.parse import urlparse
from urllib.request import urlretrieve
from jupyter_core.utils import ensure_dir_exists


DEPRECATED_ARGUMENT = object()
NBCONFIG_SECTIONS = ['common', 'notebook', 'tree', 'edit', 'terminal']


def _safe_is_tarfile(path):
    """Safe version of is_tarfile, return False on IOError.

    Returns whether the file exists and is a tarfile.

    Parameters
    ----------

    path : string
        A path that might not exist and or be a tarfile
    """
    try:
        return tarfile.is_tarfile(path)
    except IOError:
        return False


def _find_uninstall_nbextension(filename, logger=None):
    """Remove nbextension files from the first location they are found.

    Returns True if files were removed, False otherwise.
    """
    filename = cast_unicode_py2(filename)
    for nbext in jupyter_path('nbextensions'):
        path = pjoin(nbext, filename)
        if os.path.lexists(path):
            if logger:
                logger.info("Removing: %s" % path)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
            return True

    return False


def _should_copy(src, dest, logger=None):
    """Should a file be copied, if it doesn't exist, or is newer?

    Returns whether the file needs to be updated.

    Parameters
    ----------

    src : string
        A path that should exist from which to copy a file
    src : string
        A path that might exist to which to copy a file
    logger : Jupyter logger [optional]
        Logger instance to use
    """
    if not os.path.exists(dest):
        return True
    if os.stat(src).st_mtime - os.stat(dest).st_mtime > 1e-6:
        # we add a fudge factor to work around a bug in python 2.x
        # that was fixed in python 3.x: https://bugs.python.org/issue12904
        if logger:
            logger.warn("Out of date: %s" % dest)
        return True
    if logger:
        logger.info("Up to date: %s" % dest)
    return False


def _maybe_copy(src, dest, logger=None):
    """Copy a file if it needs updating.

    Parameters
    ----------

    src : string
        A path that should exist from which to copy a file
    src : string
        A path that might exist to which to copy a file
    logger : Jupyter logger [optional]
        Logger instance to use
    """
    if _should_copy(src, dest, logger=logger):
        if logger:
            logger.info("Copying: %s -> %s" % (src, dest))
        shutil.copy2(src, dest)

def validate_nbextension_python(spec, full_dest, logger=None):
    """Assess the health of an installed nbextension

    Returns a list of warnings.

    Parameters
    ----------

    spec : dict
        A single entry of _jupyter_nbextension_paths():
            [{
                'section': 'notebook',
                'src': 'mockextension',
                'dest': '_mockdestination',
                'require': '_mockdestination/index'
            }]
    full_dest : str
        The on-disk location of the installed nbextension: this should end
        with `nbextensions/<dest>`
    logger : Jupyter logger [optional]
        Logger instance to use
    """
    infos = []
    warnings = []

    section = spec.get("section", None)
    if section in NBCONFIG_SECTIONS:
        infos.append(u"  {} section: {}".format(GREEN_OK, section))
    else:
        warnings.append(u"  {}  section: {}".format(RED_X, section))

    require = spec.get("require", None)
    if require is not None:
        require_path = os.path.join(
            full_dest[0:-len(spec["dest"])],
            u"{}.js".format(require))
        if os.path.exists(require_path):
            infos.append(u"  {} require: {}".format(GREEN_OK, require_path))
        else:
            warnings.append(u"  {}  require: {}".format(RED_X, require_path))

    if logger:
        if warnings:
            logger.warning("- Validating: problems found:")
            for msg in warnings:
                logger.warning(msg)
            for msg in infos:
                logger.info(msg)
            logger.warning(u"Full spec: {}".format(spec))
        else:
            logger.info(u"- Validating: {}".format(GREEN_OK))

    return warnings


def _get_nbextension_metadata(module):
    """Get the list of nbextension paths associated with a Python module.

    Returns a tuple of (the module,             [{
        'section': 'notebook',
        'src': 'mockextension',
        'dest': '_mockdestination',
        'require': '_mockdestination/index'
    }])

    Parameters
    ----------

    module : str
        Importable Python module exposing the
        magic-named `_jupyter_nbextension_paths` function
    """
    m = import_item(module)
    if not hasattr(m, '_jupyter_nbextension_paths'):
        raise KeyError('The Python module {} is not a valid nbextension, '
                       'it is missing the `_jupyter_nbextension_paths()` method.'.format(module))
    nbexts = m._jupyter_nbextension_paths()
    return m, nbexts


def _get_nbextension_dir(user=False, sys_prefix=False, prefix=None, nbextensions_dir=None):
    """Return the nbextension directory specified

    Parameters
    ----------

    user : bool [default: False]
        Get the user's .jupyter/nbextensions directory
    sys_prefix : bool [default: False]
        Get sys.prefix, i.e. ~/.envs/my-env/share/jupyter/nbextensions
    prefix : str [optional]
        Get custom prefix
    nbextensions_dir : str [optional]
        Get what you put in
    """
    conflicting = [
        ('user', user),
        ('prefix', prefix),
        ('nbextensions_dir', nbextensions_dir),
        ('sys_prefix', sys_prefix),
    ]
    conflicting_set = ['{}={!r}'.format(n, v) for n, v in conflicting if v]
    if len(conflicting_set) > 1:
        raise ArgumentConflict(
            "cannot specify more than one of user, sys_prefix, prefix, or nbextensions_dir, but got: {}"
            .format(', '.join(conflicting_set)))
    if user:
        nbext = pjoin(jupyter_data_dir(), u'nbextensions')
    elif sys_prefix:
        nbext = pjoin(ENV_JUPYTER_PATH[0], u'nbextensions')
    elif prefix:
        nbext = pjoin(prefix, 'share', 'jupyter', 'nbextensions')
    elif nbextensions_dir:
        nbext = nbextensions_dir
    else:
        nbext = pjoin(SYSTEM_JUPYTER_PATH[0], 'nbextensions')
    return nbext


def install_nbextension(path, overwrite=False, symlink=False,
                        user=False, prefix=None, nbextensions_dir=None,
                        destination=None, verbose=DEPRECATED_ARGUMENT,
                        logger=None, sys_prefix=False
                        ):
    """Install a Javascript extension for the notebook
    
    Stages files and/or directories into the nbextensions directory.
    By default, this compares modification time, and only stages files that need updating.
    If `overwrite` is specified, matching files are purged before proceeding.
    
    Parameters
    ----------
    
    path : path to file, directory, zip or tarball archive, or URL to install
        By default, the file will be installed with its base name, so '/path/to/foo'
        will install to 'nbextensions/foo'. See the destination argument below to change this.
        Archives (zip or tarballs) will be extracted into the nbextensions directory.
    overwrite : bool [default: False]
        If True, always install the files, regardless of what may already be installed.
    symlink : bool [default: False]
        If True, create a symlink in nbextensions, rather than copying files.
        Not allowed with URLs or archives. Windows support for symlinks requires
        Vista or above, Python 3, and a permission bit which only admin users
        have by default, so don't rely on it.
    user : bool [default: False]
        Whether to install to the user's nbextensions directory.
        Otherwise do a system-wide install (e.g. /usr/local/share/jupyter/nbextensions).
    prefix : str [optional]
        Specify install prefix, if it should differ from default (e.g. /usr/local).
        Will install to ``<prefix>/share/jupyter/nbextensions``
    nbextensions_dir : str [optional]
        Specify absolute path of nbextensions directory explicitly.
    destination : str [optional]
        name the nbextension is installed to.  For example, if destination is 'foo', then
        the source file will be installed to 'nbextensions/foo', regardless of the source name.
        This cannot be specified if an archive is given as the source.
    logger : Jupyter logger [optional]
        Logger instance to use
    """
    if verbose != DEPRECATED_ARGUMENT:
        import warnings
        warnings.warn("`install_nbextension`'s `verbose` parameter is deprecated, it will have no effects and will be removed in Notebook 5.0", DeprecationWarning)

    # the actual path to which we eventually installed
    full_dest = None

    nbext = _get_nbextension_dir(user=user, sys_prefix=sys_prefix, prefix=prefix, nbextensions_dir=nbextensions_dir)
    # make sure nbextensions dir exists
    ensure_dir_exists(nbext)
    
    # forcing symlink parameter to False if os.symlink does not exist (e.g., on Windows machines running python 2)
    if not hasattr(os, 'symlink'):
        symlink = False
    
    if isinstance(path, (list, tuple)):
        raise TypeError("path must be a string pointing to a single extension to install; call this function multiple times to install multiple extensions")
    
    path = cast_unicode_py2(path)

    if path.startswith(('https://', 'http://')):
        if symlink:
            raise ValueError("Cannot symlink from URLs")
        # Given a URL, download it
        with TemporaryDirectory() as td:
            filename = urlparse(path).path.split('/')[-1]
            local_path = os.path.join(td, filename)
            if logger:
                logger.info("Downloading: %s -> %s" % (path, local_path))
            urlretrieve(path, local_path)
            # now install from the local copy
            full_dest = install_nbextension(local_path, overwrite=overwrite, symlink=symlink,
                nbextensions_dir=nbext, destination=destination, logger=logger)
    elif path.endswith('.zip') or _safe_is_tarfile(path):
        if symlink:
            raise ValueError("Cannot symlink from archives")
        if destination:
            raise ValueError("Cannot give destination for archives")
        if logger:
            logger.info("Extracting: %s -> %s" % (path, nbext))

        if path.endswith('.zip'):
            archive = zipfile.ZipFile(path)
        elif _safe_is_tarfile(path):
            archive = tarfile.open(path)
        archive.extractall(nbext)
        archive.close()
        # TODO: what to do here
        full_dest = None
    else:
        if not destination:
            destination = basename(normpath(path))
        destination = cast_unicode_py2(destination)
        full_dest = normpath(pjoin(nbext, destination))
        if overwrite and os.path.lexists(full_dest):
            if logger:
                logger.info("Removing: %s" % full_dest)
            if os.path.isdir(full_dest) and not os.path.islink(full_dest):
                shutil.rmtree(full_dest)
            else:
                os.remove(full_dest)

        if symlink:
            path = os.path.abspath(path)
            if not os.path.exists(full_dest):
                if logger:
                    logger.info("Symlinking: %s -> %s" % (full_dest, path))
                os.symlink(path, full_dest)
        elif os.path.isdir(path):
            path = pjoin(os.path.abspath(path), '') # end in path separator
            for parent, dirs, files in os.walk(path):
                dest_dir = pjoin(full_dest, parent[len(path):])
                if not os.path.exists(dest_dir):
                    if logger:
                        logger.info("Making directory: %s" % dest_dir)
                    os.makedirs(dest_dir)
                for file_name in files:
                    src = pjoin(parent, file_name)
                    dest_file = pjoin(dest_dir, file_name)
                    _maybe_copy(src, dest_file, logger=logger)
        else:
            src = path
            _maybe_copy(src, full_dest, logger=logger)

    return full_dest


def install_nbextension_python(module, overwrite=False, symlink=False,
                        user=False, sys_prefix=False, prefix=None, nbextensions_dir=None, logger=None):
    """Install an nbextension bundled in a Python package.

    Returns a list of installed/updated directories.

    See install_nbextension for parameter information."""
    m, nbexts = _get_nbextension_metadata(module)
    base_path = os.path.split(m.__file__)[0]

    full_dests = []

    for nbext in nbexts:
        src = os.path.join(base_path, nbext['src'])
        dest = nbext['dest']

        if logger:
            logger.info("Installing %s -> %s" % (src, dest))
        full_dest = install_nbextension(
            src, overwrite=overwrite, symlink=symlink,
            user=user, sys_prefix=sys_prefix, prefix=prefix, nbextensions_dir=nbextensions_dir,
            destination=dest, logger=logger
            )
        validate_nbextension_python(nbext, full_dest, logger)
        full_dests.append(full_dest)

    return full_dests



