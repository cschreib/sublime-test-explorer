import os
import logging
import json
from typing import Dict, List, Optional
from tempfile import TemporaryDirectory

from ..test_framework import (TestFramework, register_framework)
from ..test_suite import TestSuite
from ..test_data import (DiscoveredTest, DiscoveryError, TestLocation, TestData,
                         StartedTest, FinishedTest, TEST_SEPARATOR, TestStatus, TestOutput)
from .. import process
from . import common

logger = logging.getLogger('TestManager.gtest')
parser_logger = logging.getLogger('TestManagerParser.gtest')


class OutputParser:
    def __init__(self, test_data: TestData, suite_id: str, executable: str):
        self.test_data = test_data
        self.test_list = test_data.get_test_list()
        self.suite_id = suite_id
        self.executable = executable
        self.current_test: Optional[List[str]] = None

    def parse_test_id(self, line: str):
        return line[12:].strip().split(' ')[0]

    def finish_current_test(self):
        if self.current_test is not None:
            self.test_data.notify_test_finished(FinishedTest(self.current_test, TestStatus.CRASHED))
            self.current_test = None

    def close(self):
        self.finish_current_test()

    def feed(self, line: str):
        parser_logger.debug(line.rstrip())

        if line.startswith('[ RUN      ] '):
            self.finish_current_test()
            self.current_test = self.test_list.find_test_by_report_id(
                self.suite_id, self.executable, self.parse_test_id(line))
            if self.current_test is None:
                return

            self.test_data.notify_test_started(StartedTest(self.current_test))

        if self.current_test:
            self.test_data.notify_test_output(TestOutput(self.current_test, line))

        if line.startswith('[       OK ] '):
            if self.current_test is None:
                return

            self.test_data.notify_test_finished(FinishedTest(self.current_test, TestStatus.PASSED))
            self.current_test = None
        elif line.startswith('[  FAILED  ] '):
            if self.current_test is None:
                return

            self.test_data.notify_test_finished(FinishedTest(self.current_test, TestStatus.FAILED))
            self.current_test = None
        elif line.startswith('[  SKIPPED ] '):
            if self.current_test is None:
                return

            self.test_data.notify_test_finished(FinishedTest(self.current_test, TestStatus.SKIPPED))
            self.current_test = None


class GoogleTest(TestFramework):
    def __init__(self,
                 suite: TestSuite,
                 executable_pattern: str = '*',
                 env: Dict[str, str] = {},
                 cwd: Optional[str] = None,
                 args: List[str] = [],
                 discover_args: List[str] = [],
                 run_args: List[str] = [],
                 parser: str = 'default'):
        super().__init__(suite)
        self.executable_pattern = executable_pattern
        self.env = env
        self.cwd = cwd
        self.args = args
        self.discover_args = discover_args
        self.run_args = run_args
        self.parser = parser

    @staticmethod
    def get_default_settings():
        return {
            'executable_pattern': '*',
            'env': {},
            'cwd': None,
            'args': [],
            'discover_args': ['--gtest_list_tests'],
            'run_args': [],
            'parser': 'default'
        }

    @staticmethod
    def from_json(suite: TestSuite, settings: Dict):
        assert settings['type'] == 'gtest'
        return GoogleTest(suite=suite,
                          executable_pattern=settings['executable_pattern'],
                          env=settings['env'],
                          cwd=settings['cwd'],
                          args=settings['args'],
                          discover_args=settings['discover_args'],
                          run_args=settings['run_args'],
                          parser=settings['parser'])

    def discover(self) -> List[DiscoveredTest]:
        cwd = common.get_working_directory(user_cwd=self.cwd, project_root_dir=self.project_root_dir)

        errors = []
        tests = []

        with TemporaryDirectory() as temp_dir:
            def run_discovery(executable):
                output_file = os.path.join(temp_dir, 'output.json')
                exe = common.make_executable_path(executable, project_root_dir=self.project_root_dir)
                discover_args = [exe] + self.discover_args + self.args + [f'--gtest_output=json:{output_file}']
                process.get_output(discover_args, env=self.env, cwd=cwd)
                try:
                    return self.parse_discovery(output_file, executable)
                except DiscoveryError as e:
                    errors.append(e.details if e.details else str(e))
                    return []

            executables = common.discover_executables(self.executable_pattern, cwd=self.project_root_dir)
            if len(executables) == 0:
                logger.warning(f'no executable found with pattern "{self.executable_pattern}" ' +
                               f'(cwd: {self.project_root_dir})')

            for executable in executables:
                tests += run_discovery(executable)

        if errors:
            raise DiscoveryError('Error when discovering tests. See panel for more information', details=errors)

        return tests

    def parse_discovered_test(self, test: dict, suite: str, executable: str):
        # GTest reports absolute paths; make it relative to the project directory.
        file = os.path.relpath(test['file'], start=self.project_root_dir)
        line = test['line']

        path = []

        if self.suite.custom_prefix is not None:
            path += self.suite.custom_prefix.split(TEST_SEPARATOR)

        path += common.get_file_prefix(executable, path_prefix_style=self.suite.path_prefix_style)

        pretty_suite = suite
        if 'type_param' in test:
            pretty_suite = '/'.join(pretty_suite.split('/')[:-1]) + f'<{test["type_param"]}>'

        name = test['name']

        pretty_name = name
        if 'value_param' in test:
            pretty_name = '/'.join(pretty_name.split('/')[:-1]) + f'[{test["value_param"]}]'

        path += pretty_suite.split('/') + [pretty_name]

        run_id = f'{suite}.{name}'

        return DiscoveredTest(
            full_name=path, suite_id=self.suite.suite_id, run_id=run_id, report_id=run_id,
            location=TestLocation(executable=executable, file=file, line=line))

    def parse_discovery(self, output_file: str, executable: str) -> List[DiscoveredTest]:
        with open(output_file, 'r') as f:
            data = json.load(f)

        tests = []
        for suite in data['testsuites']:
            for test in suite['testsuite']:
                tests.append(self.parse_discovered_test(test, suite['name'], executable))

        return tests

    def run(self, grouped_tests: Dict[str, List[str]]) -> None:
        cwd = common.get_working_directory(user_cwd=self.cwd, project_root_dir=self.project_root_dir)

        def run_tests(executable, test_ids):
            logger.debug('starting tests from {}: "{}"'.format(executable, '" "'.join(test_ids)))

            test_filters = ':'.join(test_ids)
            exe = common.make_executable_path(executable, project_root_dir=self.project_root_dir)

            parser = common.get_generic_parser(parser=self.parser,
                                               test_data=self.test_data,
                                               suite_id=self.suite.suite_id,
                                               executable=executable)

            if parser is None:
                parser = OutputParser(self.test_data, self.suite.suite_id, executable)

            run_args = [exe] + self.run_args + self.args + ['--gtest_filter=' + test_filters]
            process.get_output_streamed(run_args,
                                        parser.feed, self.test_data.stop_tests_event,
                                        queue='gtest', ignore_errors=True, env=self.env, cwd=cwd)

            parser.close()

        for executable, test_ids in grouped_tests.items():
            run_tests(executable, test_ids)


register_framework('gtest', 'GoogleTest (C++)', GoogleTest.from_json, GoogleTest.get_default_settings())
