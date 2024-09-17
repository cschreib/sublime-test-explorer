# Inspired from https://github.com/kondratyev-nv/vscode-python-test-adapter
import pytest
import json
import os

# report.longreprtext requires >= 3.0
PYTEST_MIN_VERSION = 3

DISCOVERY_HEADER = 'SUBLIME_DISCOVERY: '
STATUS_HEADER = 'SUBLIME_STATUS: '

def get_file(item):
    try:
        # location is (file path, line, test name).
        return item.location[0]
    except:
        return item.nodeid.split('::')[0]

def get_line_number(item):
    try:
        # location is (file path, line, test name).
        # Line number is zero-based.
        return item.location[1] + 1
    except:
        # Fall back to start of the file, for lack of better information.
        return 1

def make_name(item):
    if isinstance(item, pytest.File):
        return os.path.relpath(item.path, start=os.getcwd())
    else:
        return (make_name(item.parent) + '::' + item.name) if item.parent.name != "" else item.name

collected_errors = []

def pytest_collectreport(report):
    try:
        if report.failed:
            collected_errors.append({'message': report.longreprtext})
    except:
        pass

def pytest_collection_finish(session):
    global collected_errors

    try:
        tests = [{'name': make_name(item), 'file': get_file(item), 'line': get_line_number(item)} for item in session.items]
    except:
        tests = []

    if int(pytest.__version__.split('.')[0]) < PYTEST_MIN_VERSION:
        collected_errors = [{'location': None, 'message': f'Error: Pytest {PYTEST_MIN_VERSION}.0 or later is required for this SublimeText plugin to work.'}]
        tests = []

    print('\n' + DISCOVERY_HEADER + json.dumps({'tests': tests, 'errors': collected_errors}))

@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_report_teststatus(report):
    if report.when == "call":
        print('\n' + STATUS_HEADER + json.dumps({'status': report.outcome}))
    yield

@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_protocol(item):
    print('\n' + STATUS_HEADER + json.dumps({'test': make_name(item), 'status': 'started'}))
    yield
    print('\n' + STATUS_HEADER + json.dumps({'test': make_name(item), 'status': 'finished'}))
