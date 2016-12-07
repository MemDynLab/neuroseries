from .tracker_utils import get_repo_info, in_ipynb, get_environment_yml
from .git_annex import AnnexRepo
import pandas as pd
import numpy as np
import json
_dont_check_git = True
_no_git_repo = True


# noinspection PyProtectedMember
def _get_init_info():
    # this gets all the needed information at the beginning of the run
    info = {}

    # define a random UUID
    from uuid import uuid4
    info['uuid'] = str(uuid4())

    # report the time of run start
    from time import time
    info['run_time'] = str(time())

    # get name of the entry point and the arguments
    from sys import argv
    import os.path
    if in_ipynb():
        info['entry_point'] = '###notebook'
        info['args'] = []
    else:
        info['entry_point'] = os.path.realpath(argv[0])
        info['args'] = argv[1:]

    # get git status, if it's a script, this should be completely committed,
    # if it's a notebook everything should be committed
    # except for the notebook itself (which may be committed at the save time)
    repos = []

    if _no_git_repo:
        script_repo_info = {'working_tree_dir': ''}
    else:
        script_repo_info, is_dirty, script_repo = get_repo_info(os.path.dirname(info['entry_point']))

        if not _dont_check_git and is_dirty and not in_ipynb():
            raise RuntimeError("""Running from a dirty git repository (and not from a notebook).
            Please commit your changes before running""")

        if not _dont_check_git and is_dirty:
            d = script_repo.index.diff(None)
            if len(d) > 1:
                raise RuntimeError("""Running from a dirty git repo (besides the current notebook).
                Please commit your changes before running""")

    repos.append(script_repo_info)

    # open config file, get git repos to be tracked, eventual files that need to be included in the dependencies
    # (e.g. lookup tables, etc.)
    # config file can be 1) in the current directory, named neuroseries.yml,
    # 2) in the 'project' directory (root of the containing git repo
    # 3) in the home directory as .neuroseries/config.yaml
    # 4) at the location pointed to by the variable NEUROSERIES_CONFIG

    config_candidates = ['./neuroseries.yml']

    import os.path
    config_candidates.append(os.path.join(repos[0]['working_tree_dir'], 'neuroseries.yml'))

    config_candidates.append('~/.neuroseries/config.yml')

    import os
    if 'NEUROSERIES_CONFIG' in os.environ:
        config_candidates.append(os.environ['NEUROSERIES_CONFIG'])

    import yaml
    config = {}
    for config_file in config_candidates:
        try:
            with open(os.path.expanduser(config_file)) as source:
                config = yaml.load(source)
                print('found config file at ' + config_file)
                break
        except FileNotFoundError:
            pass

    extra_repos = []
    info['config'] = config
    if 'extra_repos' in config:
        extra_repos.extend(config['extra_repos'])

    for r in extra_repos:
        script_repo_info, is_dirty, script_repo = get_repo_info(r)
        if is_dirty:
            raise RuntimeError("Dependency repository " + r + "is dirty, please commit!")
        repos.append(script_repo_info)

    info['repos'] = repos

    # get venv status
    venv = get_environment_yml()
    info['venv'] = venv

    # get os information
    import platform
    os_info = dict(platform.uname()._asdict())
    info['os'] = os_info

    # get hardware information

    import psutil
    # noinspection PyProtectedMember
    meminfo = dict(psutil.virtual_memory()._asdict())
    info['memory'] = meminfo

    return info

track_info = _get_init_info()
dependencies = []


def str_to_series(a_string):
    ba = bytearray(a_string, encoding='utf-8')
    ss = pd.Series(ba)
    return ss


def series_to_str(ss):
    bb = bytearray(ss.values.tolist())
    bs = bytes(bb)
    a_string = bs.decode()
    return a_string


# noinspection PyClassHasNoInit
class PandasHDFStoreWithTracking(pd.HDFStore):
    def get_info(self):
        ss = self['file_info']
        return series_to_str(ss)

    def put_info(self, info):
        ss = str_to_series(info)
        self.put('file_info', ss)

    def get_with_metadata(self, key):
        data = self[key]
        metadata = None
        attr = self.get_storer(key).attrs
        if hasattr(attr, 'metadata'):
            metadata = attr.metadata

        return data, metadata

    def get(self, key):
        data = super().get(key)
        attr = self.get_storer(key).attrs
        if hasattr(attr, 'metadata') and attr.metadata['class'] == 'ndarray':
            data = data.values
        return data

    def put_with_metadata(self, key, value, metadata, **kwargs):
        self.put(key, value, **kwargs)
        attr = self.get_storer(key).attrs
        if hasattr(attr, 'metadata'):
            ex_metadata = attr.metadata
            metadata.update(ex_metadata)

        self.get_storer(key).attrs.metadata = metadata

    def put(self, key, value, **kwargs):
        if isinstance(value, np.ndarray):
            if value.ndim <= 2:
                value = pd.DataFrame(value)
            else:
                value = pd.Panel(value)
            super().put(key, value, **kwargs)
            metadata = {'class': 'ndarray'}
            self.get_storer(key).attrs.metadata = metadata
        else:
            super().put(key, value, **kwargs)


class FilesBackend(object):
    def __init__(self):
        pass

    def fetch_file(self, filename):
        pass

    @staticmethod
    def repo_info():
        return {}

    def save_metadata(self, filename, info):
        pass

    def commit_file(self, filename):
        pass


default_backend = None


class AnnexJsonBackend(object):
    def __init__(self, dirname=None, clone_from=None, description=''):
        import os
        if dirname is None:
            dirname = os.getcwd()
        dirname = os.path.expanduser(dirname)
        self.repo = AnnexRepo(dirname, clone_from=clone_from, description=description)

    def fetch_file(self, filename):
        self.repo.get(filename)

    # noinspection PyMethodMayBeStatic
    def repo_info(self):
        repo_info = {'backend': 'AnnexJsonBackend'}
        # TODO add working tree and remote
        return repo_info

    def save_metadata(self, filename, info):
        self.repo.add_annex(filename)
        hash_file = self.repo.lookupkey(filename)
        info['hash'] = hash_file
        import os.path
        root, _ = os.path.splitext(filename)
        json_file = root + '.json'
        with open(json_file, 'w') as f:
            f.write(json.dumps(info))
        f.close()
        self.repo.add(json_file)

    def commit(self, filename, message_add=None):
        import sys
        message = 'Ran ' + sys.argv[0] + '. Added ' + filename + '. '
        if message_add:
            message += message_add
        self.repo.commit(message)


class TrackingStore(object):
    # what it has to do
    # at construction
    # 1) fetch data wherever they are (backend)
    # 2) generate material store, material store should be able to handle pandas object (e.g. HDFStore)
    # 3) if read: get the tracking info if present add it to dependencies,
    # if not generate file fingerprint, store that as tracking info
    # 4) if write: dump information
    # file information is a dict {'file': filename, 'hash': an hash for the file, for example the SHA256 from git-annex,
    # 'date-created': a date string, 'run': track_info, 'dependencies': all the dependencies,
    # 'repo_info': information useful for the backend to retrieve the file
    # 'variables': a dict of variable names and info (as for example given by
    # information is serialized as json and stored in the material store (backend)
    # information also stored in json (text) metafile. before the file is closed,
    # the hash will be the placeholder 'NULL'.
    # it will be replaced in the metafile by the hash at closure

    # material store must
    # handle pandas objects, with metadata. Must be able to store (ASCII) info string
    # must be able to write and read with or without metadata (for pandas)
    # it can only do write and read, no append (enforced in this class)

    # backend must
    # fetch file (ensure availability)
    # provide hash
    # handle metafile info (or other mechanism). metafile info
    # commit file and metafiles (for example, at closure)
    # for git-annex that would mean: data files in the annex, metafiles in the git repo #TODO

    # backend is defined once per run and remains a property of run TODO

    def __init__(self, filename, mode='r', backend=None, store_type=PandasHDFStoreWithTracking):  # TODO change backend
        if mode not in ['r', 'w']:
            raise ValueError('mode must be "w" or "r"')
        self.mode = mode
        self.filename = filename
        if backend:
            self.backend = backend
        else:
            self.backend = default_backend
        self.backend.fetch_file(filename)
        self.store = store_type(filename, mode)

        if mode == 'r':
            self.info = json.loads(self.store.get_info())
            dependencies.append(self.info)
        else:
            import datetime
            repo_info = self.backend.repo_info()
            self.info = {'run': track_info, 'date_created': datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         'dependencies': dependencies, 'file': filename, 'hash': 'NULL', 'repo_info': repo_info,
                         'variables': {}}
            self.store.put_info(json.dumps(self.info))

    def close(self):
        self.store.put_info(json.dumps(self.info))
        self.store.close()
        self.backend.save_metadata(self.filename, self.info)
        self.backend.commit(self.filename)

    def get(self, key):
        if self.mode == 'w':
            raise IOError("store is open in write mode")
        return self.store.get(key)

    def has_metadata(self, key):
        if self.mode == 'w':
            raise IOError("store is open in write mode")
        (_, metadata) = self.store.get_with_metadata(key)
        return metadata is not None

    def get_with_metadata(self, key):
        if self.mode == 'w':
            raise IOError("store is open in write mode")
        return self.store.get_with_metadata(key)

    def keys(self):
        return self.store.keys()

    def put(self, key, value, metadata=None, **kwargs):
        if self.mode == 'r':
            raise IOError('store is open in read mode')
        self.info['variables'][key] = self.get_variable_info(key, value)

        self.store.put_with_metadata(key, value, metadata, **kwargs)

    def append(self, key, value, **keyargs):
        pass

    def get_variable_info(self, key, var):
        info = "Object of class: " + type(var).__name__ + '\n'
        try:
            i = self.store[key].info()
            info += i
        except AttributeError:
            try:
                i = var.info()
                info += i
            except AttributeError:
                try:
                    s = str(var.shape)
                    info += 'Shape: ' + s
                except AttributeError:
                    import warnings
                    warnings.warn("Cannot determine info for variable. ")
        return info

HDFStore = TrackingStore
