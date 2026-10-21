from worker.judger import _SOURCE_FILES, _files, compare_outputs


def test_compare_outputs_ignores_crlf_and_trailing_blank_lines():
    assert compare_outputs("a\r\nb\r\n\r\n", "a\nb\n")
    assert compare_outputs("1 2\n", "1 2   \n")
    assert compare_outputs("", "\n\n")


def test_compare_outputs_preserves_internal_spaces():
    assert not compare_outputs("a  b", "a b")
    assert not compare_outputs("a\nb", "b\na")


def test_files_contain_source_and_input_but_never_expected_output():
    source = "print('hello')\n"
    files = dict(_files(source, "input\n", "py"))
    assert list(files) == [_SOURCE_FILES["py"], "input.txt"]
    assert files["input.txt"].decode() == "input\n"
    assert files[_SOURCE_FILES["py"]].decode() == source
