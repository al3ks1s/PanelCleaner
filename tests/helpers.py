from importlib import resources
from tests import mock_files


def read_mock_file(file_name: str, module=mock_files) -> str:
    with resources.files(module) as mock_path:
        path = mock_path / file_name
        file_contents = path.read_text()
    return file_contents
