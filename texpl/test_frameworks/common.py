from typing import Optional, List
import os
import xml.sax
from abc import ABC, abstractmethod
import logging

from ..test_data import TestData
from .teamcity import OutputParser as TeamcityOutputParser


def get_working_directory(user_cwd: Optional[str], project_root_dir: str):
    if user_cwd is not None:
        cwd = user_cwd
        if not os.path.isabs(cwd):
            cwd = os.path.join(project_root_dir, cwd)
    else:
        cwd = project_root_dir

    return cwd


def make_executable_path(executable: str, project_root_dir: str):
    return os.path.join(project_root_dir, executable) if not os.path.isabs(executable) else executable


def get_generic_parser(parser: str, test_data: TestData, framework_id: str, executable: str):
    if parser == 'teamcity':
        return TeamcityOutputParser(test_data, framework_id, executable)

    return None


def make_header(text, length=64, pattern='='):
    remaining = max(0, length - len(text) - 2)
    return f"{pattern*(remaining//2)} {text} {pattern*(remaining - remaining//2)}"


class XmlParser(ABC):
    @abstractmethod
    def startElement(self, name, attrs) -> None:
        pass

    @abstractmethod
    def endElement(self, name, content) -> None:
        pass

    @abstractmethod
    def output(self, content) -> None:
        pass


xml_parser_logger = logging.getLogger('TestExplorerParser.xml-base')


class XmlStreamHandler(xml.sax.handler.ContentHandler):
    def __init__(self, parser: XmlParser, controlled_tags: List[str] = []):
        self.parser = parser
        self.controlled_tags = controlled_tags

        self.current_element: List[str] = []
        self.content = {}

    def clean_xml_content(self, content, tag):
        # Remove first and last entry; will be line jump and indentation whitespace, ignored.
        if not tag in content:
            return ''

        returned_content = content[tag]
        del content[tag]

        if len(returned_content) <= 2:
            return ''

        return ''.join(returned_content[1:-1])

    def startElement(self, name, attrs):
        if len(self.current_element) > 0 and self.current_element[-1] not in self.controlled_tags:
            content = self.clean_xml_content(self.content, self.current_element[-1])
            self.parser.output(content)

        attrs_str = ', '.join(['"{}": "{}"'.format(k, v) for k, v in attrs.items()])
        xml_parser_logger.debug('startElement(' + name + ', ' + attrs_str + ')')
        self.current_element.append(name)

        self.parser.startElement(name, attrs)

    def endElement(self, name):
        if name not in self.controlled_tags:
            content = self.clean_xml_content(self.content, name)
            self.parser.output(content)

        xml_parser_logger.debug('endElement(' + name + ')')
        self.current_element.pop()

        self.parser.endElement(name, self.clean_xml_content(self.content, name))

    def characters(self, content):
        xml_parser_logger.debug('characters(' + content + ')')
        if len(self.current_element) > 0:
            self.content.setdefault(self.current_element[-1], []).append(content)

            if self.current_element[-1] not in self.controlled_tags:
                content = self.content[self.current_element[-1]]
                if len(content) > 1 and len(content[-1].strip()) > 0:
                    output_content = ''.join(content[1:])
                    del content[1:]
                    self.parser.output(output_content)
