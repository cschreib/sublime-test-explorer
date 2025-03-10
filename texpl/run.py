# coding: utf-8
import time
import logging
import os
from typing import List
from functools import partial
import traceback

import sublime
from sublime_plugin import WindowCommand, TextCommand

from .helpers import TestDataHelper
from .list import TestExplorerTextCmd
from .test_framework import TestFramework
from .discover import NO_FRAMEWORK_CONFIGURED
from .util import SettingsHelper
from .test_data import TestData, TestList, TestItem, StartedRun, FinishedRun, test_name_to_path, ROOT_NAME

logger = logging.getLogger('TestExplorer.runner')

TEST_STOP_CONFIRM_DIALOG = ("You are about to stop all currently-running tests. Are you sure?")

CANNOT_START_WHILE_RUNNING_DIALOG = ("Tests are currently running; please wait or stop the tests "
                                     "before running new tests.")


class TestRunHelper(SettingsHelper):
    def get_frameworks(self, data: TestData, project: str):
        frameworks_json = self.get_setting('frameworks')
        if not frameworks_json:
            # TODO: change this into a "Do you want to configure a framework now?"
            # Then propose a dropdown list of all available frameworks, and init to default.
            # Also add a command to init a new framework to default.
            sublime.error_message(NO_FRAMEWORK_CONFIGURED)
            return None

        root_dir = os.path.dirname(project)
        return [TestFramework.from_json(data, root_dir, f) for f in frameworks_json]

    def refresh_loop(self):
        if not self.running:
            return

        sublime.run_command('test_explorer_refresh_all', {'data_location': self.data_location})

        sublime.set_timeout(self.refresh_loop, self.refresh_interval)

    def run_tests(self, data: TestData, test_list: TestList, frameworks: List[TestFramework], tests: List[str]):
        try:
            settings = self.get_settings()
            self.refresh_interval = settings.get('view_refresh_interval', 0.1) * 1000
            test_ids = {}
            test_paths = []

            def add_test(path: List[str], item: TestItem):
                path = path + [item.name] if item.name != ROOT_NAME or len(path) > 0 else path
                if item.children is not None:
                    for child in item.children.values():
                        add_test(path, child)
                else:
                    assert item.location is not None
                    test_paths.append(path)
                    test_ids.setdefault(item.framework_id, {}).setdefault(
                        item.location.executable, []).append(item.run_id)

            for test in tests:
                logger.debug(f'running {test}...')
                path = test_name_to_path(test)
                item = test_list.find_test(path)
                if not item:
                    logger.warning(f'{test} not found in list')
                    continue

                add_test(path[:-1], item)

            logger.info(f'collected {len(test_paths)} tests')
            start = time.time()

            data.notify_run_started(StartedRun(test_paths))
            sublime.run_command('test_explorer_refresh_all', {'data_location': data.location})

            self.running = True
            self.data_location = data.location
            sublime.set_timeout(self.refresh_loop, self.refresh_interval)

            try:
                for framework_id, grouped_tests in test_ids.items():
                    logger.debug(f'running {len(grouped_tests)} tests for {framework_id}...')
                    framework = next((f for f in frameworks if f.get_id() == framework_id), None)
                    if framework is None:
                        logger.warning(f'{framework_id} not found in frameworks')
                        continue

                    framework.run(grouped_tests)
                    logger.debug(f'done.')
            finally:
                data.notify_run_finished(FinishedRun(test_paths))
                self.running = False
                sublime.run_command('test_explorer_refresh_all', {'data_location': data.location})

            end = time.time()
            logger.info(f'test run duration: {end - start}')

        except Exception as e:
            logger.error("error when running tests: %s\n%s", e, traceback.format_exc())


class TestExplorerStartSelectedCommand(TextCommand, TestDataHelper, TestRunHelper, TestExplorerTextCmd):

    def is_visible(self):
        return False

    def run(self, edit):
        project = self.get_project()
        if not project:
            return

        data = self.get_test_data()
        if not data:
            return

        if data.is_running_tests():
            sublime.error_message(CANNOT_START_WHILE_RUNNING_DIALOG)
            return

        frameworks = self.get_frameworks(data, project)
        if frameworks is None:
            return

        test_list = data.get_test_list()

        tests = self.get_selected_tests()
        if len(tests) > 0:
            sublime.set_timeout_async(partial(self.run_tests, data, test_list, frameworks, tests))
            return

        tests = self.get_selected_folders()
        if len(tests) > 0:
            sublime.set_timeout_async(partial(self.run_tests, data, test_list, frameworks, tests))
            return


class TestExplorerStartCommand(WindowCommand, TestDataHelper, TestRunHelper, TestExplorerTextCmd):

    def run(self, start='all'):
        project = self.get_project()
        if not project:
            return

        data = self.get_test_data()
        if not data:
            return

        if data.is_running_tests():
            sublime.error_message(CANNOT_START_WHILE_RUNNING_DIALOG)
            return

        frameworks = self.get_frameworks(data, project)
        if frameworks is None:
            return

        test_list = data.get_test_list()

        if start == "one":
            choices = [t.full_name for t in test_list.tests()]
            if len(choices) == 0:
                return

            self.window.show_quick_panel(choices, partial(self.run_one_test, data,
                                                          test_list, frameworks, choices), sublime.MONOSPACE_FONT)
        elif start == "all":
            sublime.set_timeout_async(partial(self.run_tests, data, test_list, frameworks, ['']))

    def run_one_test(self, data: TestData, test_list: TestList,
                     frameworks: List[TestFramework], choices: List[str], test_id: int):
        sublime.set_timeout_async(partial(self.run_tests, data, test_list, frameworks, [choices[test_id]]))


class TestExplorerStopCommand(WindowCommand, TestDataHelper):

    def run(self):
        data = self.get_test_data()
        if not data:
            return

        if not data.is_running_tests():
            return

        if sublime.ok_cancel_dialog(TEST_STOP_CONFIRM_DIALOG, "Stop tests"):
            data.stop_tests_event.set()
