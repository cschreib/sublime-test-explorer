import os
import logging
import glob
import xml.etree.ElementTree as ET
import xml.sax
from typing import Dict, List, Optional
from functools import partial

from ..test_framework import TestFramework, register_framework
from ..test_data import DiscoveredTest, DiscoveryError, TestLocation, TestData, StartedTest, FinishedTest, TEST_SEPARATOR, TestStatus, TestOutput
from ..cmd import Cmd

logger = logging.getLogger('TestExplorer.doctest-cpp')
parser_logger = logging.getLogger('TestExplorerParser.doctest-cpp')

def clean_xml_content(content, tag):
    # Remove first and last entry; will be line jump and indentation whitespace, ignored.
    if not tag in content:
        return ''

    returned_content = content[tag]
    del content[tag]

    if len(returned_content) <= 2:
        return ''
    return ''.join(returned_content[1:-1])

# The content inside these tags is controlled by doctest, don't assume it is output.
controlled_tags = ['Info', 'Original', 'Expanded', 'Exception']

class ResultsStreamHandler(xml.sax.handler.ContentHandler):
    def __init__(self, test_data: TestData, framework: str, executable: str, test_ids: List[str]):
        self.test_data = test_data
        self.test_list = test_data.get_test_list()
        self.framework = framework
        self.executable = executable
        self.test_ids = test_ids

        self.current_test: Optional[List[str]] = None
        self.current_element: List[str] = []
        self.content = {}
        self.has_output = False
        self.current_expression: Optional[dict] = None
        self.current_sections = []
        self.current_infos = []
        self.current_exception: Optional[dict] = None

    def startElement(self, name, attrs):
        if len(self.current_element) > 0 and self.current_element[-1] not in controlled_tags:
            content = clean_xml_content(self.content, self.current_element[-1])
            if self.current_test is not None:
                self.test_data.notify_test_output(TestOutput(self.current_test, content))

        attrs_str = ', '.join(['"{}": "{}"'.format(k, v) for k, v in attrs.items()])
        parser_logger.debug('startElement(' + name + ', ' + attrs_str + ')')
        self.current_element.append(name)

        if name == 'TestCase':
            test_id = attrs['name']
            if not test_id in self.test_ids:
                # doctest always outputs a TestCase element for all tests, even if they are not
                # run; they are marked as "skipped". We don't want that to be interpreted as an
                # actual skipped test, it is just that the test has not run. Sadly there is no
                # distinction in the XML output between the two, so we have to manually filter out
                # results for tests that we did not intend to run...
                return

            self.current_test = self.test_list.find_test_by_run_id(self.framework, self.executable, test_id)
            if self.current_test is None:
                return

            self.test_data.notify_test_started(StartedTest(self.current_test))

            if 'skipped' in attrs and attrs['skipped'] == 'true':
                self.test_data.notify_test_finished(FinishedTest(self.current_test, TestStatus.SKIPPED))
                self.current_test = None
        elif name == 'OverallResultsAsserts':
            if self.current_test is None:
                return

            if 'test_case_success' in attrs and attrs['test_case_success'] == 'true':
                status = TestStatus.PASSED
            else:
                status = TestStatus.FAILED

            self.test_data.notify_test_finished(FinishedTest(self.current_test, status))
            self.current_test = None
            self.has_output = False
        elif name == 'Expression':
            self.current_expression = attrs
        elif name == 'Exception':
            self.current_exception = attrs
        elif name == 'SubCase':
            self.current_sections.append(attrs)

    def endElement(self, name):
        if name not in controlled_tags:
            content = clean_xml_content(self.content, name)
            if self.current_test is not None:
                self.test_data.notify_test_output(TestOutput(self.current_test, content))

        parser_logger.debug('endElement(' + name + ')')
        self.current_element.pop()

        if name == 'Expression':
            if self.current_test is None or self.current_expression is None:
                return

            sep = '-'*64 + '\n'

            original = clean_xml_content(self.content, 'Original').strip()
            expanded = clean_xml_content(self.content, 'Expanded').strip()

            file = self.current_expression["filename"]
            line = self.current_expression["line"]
            result = 'FAILED' if self.current_expression["success"] == 'false' else 'PASSED'
            check = self.current_expression["type"]
            subcases = ''.join([f'  in subcase "{s["name"]}"\n' for s in self.current_sections])
            infos = ''.join([f'  with "{i}"\n' for i in self.current_infos])

            self.test_data.notify_test_output(TestOutput(self.current_test,
                f'{sep}{result}\n  at {file}:{line}\n{subcases}{infos}\nExpected: {check}({original})\nActual:   {expanded}\n{sep}'))

            self.has_output = True
            self.current_expression = None
            self.current_infos = []
        elif name == 'Exception':
            if self.current_test is None or self.current_exception is None:
                return

            sep = '-'*64 + '\n'

            message = clean_xml_content(self.content, 'Exception').strip()
            result = 'EXCEPTION' if self.current_exception["crash"] == 'false' else 'CRASH'
            subcases = ''.join([f'  in subcase "{s["name"]}"\n' for s in self.current_sections])
            infos = ''.join([f'  with "{i}"\n' for i in self.current_infos])

            self.test_data.notify_test_output(TestOutput(self.current_test,
                f'{sep}{result}\n{subcases}{infos}{message}\n{sep}'))

            self.has_output = True
            self.current_exception = None
            self.current_infos = []
        elif name == 'SubCase':
            self.current_sections.pop()
        elif name == 'Info':
            self.current_infos.append(clean_xml_content(self.content, 'Info').strip())
        elif name == 'TestCase':
            self.content = {}

    def characters(self, content):
        parser_logger.debug('characters:' + content)
        if len(self.current_element) > 0:
            if self.current_test is None:
                return

            self.content.setdefault(self.current_element[-1], []).append(content)

            if self.current_element[-1] not in controlled_tags:
                content = self.content[self.current_element[-1]]
                if len(content) > 1 and len(content[-1].strip()) > 0:
                    self.test_data.notify_test_output(TestOutput(self.current_test, ''.join(content[1:])))
                    del content[1:]

class DoctestCpp(TestFramework, Cmd):
    def __init__(self, test_data: TestData,
                       project_root_dir: str,
                       framework_id: str = '',
                       executable_pattern: str = '*',
                       env: Dict[str,str] = {},
                       cwd: Optional[str] = None,
                       args: List[str] = [],
                       path_prefix_style: str = 'full',
                       custom_prefix: Optional[str] = None):
        super().__init__(test_data, project_root_dir)
        self.test_data = test_data
        self.framework_id = framework_id
        self.executable_pattern = executable_pattern
        self.env = env
        self.cwd = cwd
        self.args = args
        self.path_prefix_style = path_prefix_style
        self.custom_prefix = custom_prefix

    @staticmethod
    def from_json(test_data: TestData, project_root_dir: str, json_data: Dict):
        assert json_data['type'] == 'catch2'
        return DoctestCpp(test_data=test_data,
                          project_root_dir=project_root_dir,
                          framework_id=json_data['id'],
                          executable_pattern=json_data.get('executable_pattern', '*'),
                          env=json_data.get('env', {}),
                          cwd=json_data.get('cwd', None),
                          args=json_data.get('args', []),
                          path_prefix_style=json_data.get('path_prefix_style', 'full'),
                          custom_prefix=json_data.get('custom_prefix', None))

    def get_id(self):
        return self.framework_id

    def get_working_directory(self):
        # Set up current working directory. Default to the project root dir.
        if self.cwd is not None:
            cwd = self.cwd
            if not os.path.isabs(cwd):
                cwd = os.path.join(self.project_root_dir, cwd)
        else:
            cwd = self.project_root_dir

        return cwd

    def make_executable_path(self, executable):
        return os.path.join(self.project_root_dir, executable) if not os.path.isabs(executable) else executable

    def discover(self) -> List[DiscoveredTest]:
        cwd = self.get_working_directory()

        errors = []
        tests = []

        def run_discovery(executable):
            discover_args = [self.make_executable_path(executable), '-r=xml', '-ltc', '--no-skip']
            output = self.cmd_string(discover_args + self.args, env=self.env, cwd=cwd)
            try:
                return self.parse_discovery(output, executable)
            except DiscoveryError as e:
                errors.append(e.details if e.details else str(e))
                return []

        if '*' in self.executable_pattern:
            old_cwd = os.getcwd()
            os.chdir(self.project_root_dir)
            executables = [e for e in glob.glob(self.executable_pattern)]
            os.chdir(old_cwd)
            if len(executables) == 0:
                logger.warning(f'no executable found with pattern "{self.executable_pattern}" (cwd: {self.project_root_dir})')

            for executable in executables:
                tests += run_discovery(executable)
        else:
            tests += run_discovery(self.executable_pattern)

        if errors:
            raise DiscoveryError('Error when discovering tests. See panel for more information', details=errors)

        return tests

    def parse_discovered_test(self, test: ET.Element, executable: str):
        # Make file path relative to project directory.
        file = test.attrib.get('filename')
        assert file is not None

        file = os.path.relpath(file, start=self.project_root_dir)

        line = test.attrib.get('line')
        assert line is not None

        path = []

        if self.custom_prefix is not None:
            path += self.custom_prefix.split(TEST_SEPARATOR)

        if self.path_prefix_style == 'full':
            path += os.path.normpath(executable).split(os.sep)
        elif self.path_prefix_style == 'basename':
            path.append(os.path.basename(executable))
        elif self.path_prefix_style == 'none':
            pass

        suite = test.attrib.get('testsuite')
        if suite:
            path.append(suite)

        name = test.attrib.get('name')
        assert name is not None

        path.append(name)

        return DiscoveredTest(
            full_name=path, framework_id=self.framework_id, run_id=name,
            location=TestLocation(executable=executable, file=file, line=int(line)))

    def parse_discovery(self, output: str, executable: str) -> List[DiscoveredTest]:
        tests = []
        for t in ET.fromstring(output):
            if t.tag == 'TestCase':
                tests.append(self.parse_discovered_test(t, executable))

        return tests

    def run(self, grouped_tests: Dict[str, List[str]]) -> None:
        cwd = self.get_working_directory()

        def run_tests(executable, test_ids):
            logger.debug('starting tests from {}: "{}"'.format(executable, '" "'.join(test_ids)))

            test_filters = ','.join(test.replace(',','\\,') for test in test_ids)
            run_args = [self.make_executable_path(executable), '-r=xml', '-tc=' + test_filters]

            parser = xml.sax.make_parser()
            parser.setContentHandler(ResultsStreamHandler(self.test_data, self.framework_id, executable, test_ids))

            def stream_reader(parser, line):
                parser.feed(line)

            self.cmd_streamed(run_args + self.args, partial(stream_reader, parser), self.test_data.stop_tests_event,
                queue='doctest-cpp', ignore_errors=True, env=self.env, cwd=cwd)

        for executable, test_ids in grouped_tests.items():
            run_tests(executable, test_ids)


register_framework('doctest-cpp', DoctestCpp.from_json)
