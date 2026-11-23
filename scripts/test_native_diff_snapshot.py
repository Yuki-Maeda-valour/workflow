"""S3Dの比較器自身を通常回帰へ接続する。外部ビルドの指定は不要。"""
import importlib.util
from pathlib import Path
import sys


def load_tests(loader, standard_tests, pattern):
    directory = Path(__file__).resolve().parents[1] / 'native/ci'
    sys.path.insert(0, str(directory))
    try:
        name = 's3d_comparator_tests'
        spec = importlib.util.spec_from_file_location(name, directory / 'test_diff_snapshot.py')
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return loader.loadTestsFromModule(module)
    finally:
        sys.path.remove(str(directory))
