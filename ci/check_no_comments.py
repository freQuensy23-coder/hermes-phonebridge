import ast
import io
from pathlib import Path
import re
import subprocess
import sys
import tokenize

import yaml


HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
SHELL_WORD_BREAKS = " \t\n;&|()<>"


def line_of(text, index):
    return text.count("\n", 0, index) + 1


def python_comments(text):
    found = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT and not (token.start[0] == 1 and token.string.startswith("#!")):
            found.append(token.start[0])
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            found.append(node.lineno)
    return found


def c_style_comments(text):
    found = []
    stack: list[list] = [["code", 0, False]]
    index = text.find("\n") if text.startswith("#!") else 0
    index = len(text) if index < 0 else index
    while index < len(text):
        mode = stack[-1]
        char = text[index]
        if mode[0] == "code":
            if text.startswith("//", index):
                found.append(line_of(text, index))
                end = text.find("\n", index)
                index = len(text) if end < 0 else end
            elif text.startswith("/*", index):
                found.append(line_of(text, index))
                depth = 1
                index += 2
                while index < len(text) and depth:
                    if text.startswith("/*", index):
                        depth += 1
                        index += 2
                    elif text.startswith("*/", index):
                        depth -= 1
                        index += 2
                    else:
                        index += 1
            elif text.startswith('"""', index):
                stack.append(["raw", 0, False])
                index += 3
            elif char in "\"'":
                stack.append([char, 0, False])
                index += 1
            elif char == "{":
                mode[1] += 1
                index += 1
            elif char == "}":
                if mode[2] and mode[1] == 0:
                    stack.pop()
                else:
                    mode[1] -= 1
                index += 1
            else:
                index += 1
        elif mode[0] == "raw":
            if text.startswith('"""', index):
                index += 3
                while index < len(text) and text[index] == '"':
                    index += 1
                stack.pop()
            elif text.startswith("${", index):
                stack.append(["code", 0, True])
                index += 2
            else:
                index += 1
        elif char == "\\":
            index += 2
        elif char == mode[0] or char == "\n":
            stack.pop()
            index += 1
        elif mode[0] == '"' and text.startswith("${", index):
            stack.append(["code", 0, True])
            index += 2
        else:
            index += 1
    return found


def xml_comments(text):
    visible = re.sub(r"<!\[CDATA\[.*?\]\]>", lambda match: "\n" * match.group(0).count("\n"), text, flags=re.S)
    return [line_of(visible, match.start()) for match in re.finditer("<!--", visible)]


def shell_comments(text):
    found = []
    pending = []
    quote = ""
    index = text.find("\n") if text.startswith("#!") else 0
    index = len(text) if index < 0 else index
    while index < len(text):
        char = text[index]
        if quote == "'":
            quote = "" if char == "'" else quote
            index += 1
        elif quote in ('"', "$'"):
            if char == "\\":
                index += 2
                continue
            if char == quote[-1]:
                quote = ""
            index += 1
        elif char == "\\":
            index += 2
        elif text.startswith("$'", index):
            quote = "$'"
            index += 2
        elif char in "\"'":
            quote = char
            index += 1
        elif char == "#" and (index == 0 or text[index - 1] in SHELL_WORD_BREAKS):
            found.append(line_of(text, index))
            end = text.find("\n", index)
            index = len(text) if end < 0 else end
        elif text.startswith("<<", index) and not text.startswith("<<<", index) and (match := HEREDOC.match(text, index)):
            pending.append(match.group(2))
            index = match.end()
        elif char == "\n" and pending:
            index += 1
            for delimiter in pending:
                while index < len(text):
                    end = text.find("\n", index)
                    end = len(text) if end < 0 else end
                    line = text[index:end]
                    index = end + 1
                    if line.strip() == delimiter:
                        break
            pending = []
        else:
            index += 1
    return found


def toml_comments(text):
    found = []
    index = 0
    while index < len(text):
        for delimiter in ('"""', "'''"):
            if text.startswith(delimiter, index):
                index += 3
                while index < len(text) and not text.startswith(delimiter, index):
                    index += 2 if delimiter == '"""' and text[index] == "\\" else 1
                index += 3
                break
        else:
            char = text[index]
            if char in "\"'":
                index += 1
                while index < len(text) and text[index] not in (char, "\n"):
                    index += 2 if char == '"' and text[index] == "\\" else 1
                index += 1
            elif char == "#":
                found.append(line_of(text, index))
                end = text.find("\n", index)
                index = len(text) if end < 0 else end
            else:
                index += 1
    return found


def run_scripts(text):
    pending = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
    while pending:
        node = pending.pop()
        if isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                if isinstance(key, yaml.ScalarNode) and key.value == "run" and isinstance(value, yaml.ScalarNode):
                    yield value
                pending.extend((key, value))
        elif isinstance(node, yaml.SequenceNode):
            pending.extend(node.value)


def yaml_comments(text):
    found: list[int] = []
    position = 0
    for token in yaml.scan(text, Loader=yaml.SafeLoader):
        start = token.start_mark.index
        found.extend(line_of(text, position + match.start()) for match in re.finditer("#", text[position:start]))
        if isinstance(token, yaml.ScalarToken) and token.style in ("|", ">"):
            header_end = text.find("\n", start)
            if "#" in text[start:header_end if header_end >= 0 else len(text)]:
                found.append(line_of(text, start))
        position = max(position, token.end_mark.index)
    for node in run_scripts(text):
        first = node.start_mark.line + (2 if node.style in ("|", ">") else 1)
        found.extend(first + line - 1 for line in shell_comments(node.value))
    return found


def marker_comments(markers):
    def scan(text):
        return [number for number, line in enumerate(text.splitlines(), 1) if line.lstrip().lower().startswith(markers)]
    return scan


SUFFIXES = {
    ".py": python_comments,
    ".kt": c_style_comments,
    ".kts": c_style_comments,
    ".java": c_style_comments,
    ".xml": xml_comments,
    ".yml": yaml_comments,
    ".yaml": yaml_comments,
    ".sh": shell_comments,
    ".bash": shell_comments,
    ".toml": toml_comments,
    ".ini": marker_comments(("#", ";")),
    ".cfg": marker_comments(("#", ";")),
    ".service": marker_comments(("#", ";")),
    ".properties": marker_comments(("#", "!")),
    ".in": marker_comments(("#",)),
    ".bat": marker_comments(("rem ", "@rem ", "::")),
    ".cmd": marker_comments(("rem ", "@rem ", "::")),
}
NAMES = {
    ".gitignore": marker_comments(("#",)),
    ".dockerignore": marker_comments(("#",)),
}


def scanner_for(path, text):
    if path.name in NAMES:
        return NAMES[path.name]
    if path.name == "Dockerfile" or path.name.startswith("Dockerfile.") or path.suffix == ".dockerfile":
        return shell_comments
    if re.fullmatch(r"requirements[\w.-]*\.txt", path.name):
        return shell_comments
    if path.suffix in SUFFIXES:
        return SUFFIXES[path.suffix]
    if not path.suffix and text.startswith("#!"):
        shebang = text.split("\n", 1)[0]
        if "python" in shebang:
            return python_comments
        if re.search(r"\b(ba|z|da)?sh\b", shebang):
            return shell_comments
    return None


def tracked_files():
    output = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True).stdout
    return [Path(name) for name in output.decode().split("\0") if name]


def scan(paths):
    findings: list[str] = []
    checked = 0
    for path in paths:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        scanner = scanner_for(path, text)
        if scanner is None:
            continue
        checked += 1
        findings.extend(f"{path.as_posix()}:{line}" for line in sorted(set(scanner(text))))
    return checked, findings


def main(arguments):
    checked, findings = scan([Path(argument) for argument in arguments] or tracked_files())
    for finding in findings:
        print(f"{finding}: comments are forbidden")
    if findings:
        return 1
    print(f"No comments in {checked} files")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
