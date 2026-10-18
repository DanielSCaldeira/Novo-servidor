"""Narrow ZIP edit: set only the single instance's boot disk to 50 GB / 10 VPUs."""
import io
import re
import zipfile

from scripts.safety import require

LEX = re.compile(r'\s+|\#[^\n]*|//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|<<|[A-Za-z_][A-Za-z0-9_-]*|[{}]|.', re.S)


def tokens(text):
    result = []
    for match in LEX.finditer(text):
        value = match.group()
        if value.isspace() or value.startswith(('#', '//', '/*')):
            continue
        require(value != '<<', 'Heredoc na configuração: revisão manual necessária para editar o disco.')
        result.append((value, match.start(), match.end()))
    return result


def closing(items, start):
    depth = 0
    for i in range(start, len(items)):
        depth += (items[i][0] == '{') - (items[i][0] == '}')
        if depth == 0:
            return i
    raise ValueError('Invalid block')


def fix_disk_zip(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = [(info, archive.read(info.filename)) for info in archive.infolist()]
    candidates = []
    for entry_index, (info, content) in enumerate(entries):
        if not info.filename.endswith('.tf'):
            continue
        text = content.decode('utf-8-sig')
        items = tokens(text)
        for i in range(len(items)-3):
            if items[i][0] == 'resource' and items[i+1][0] == '"oci_core_instance"' and items[i+3][0] == '{':
                end = closing(items, i+3)
                candidates.append((entry_index, text, items, i+3, end, content.startswith(b'\xef\xbb\xbf')))
    require(len(candidates) == 1, 'Edição exige exatamente um resource oci_core_instance em arquivo .tf.')
    entry_index, text, items, begin, end, bom = candidates[0]
    sources = []
    depth = 1
    for i in range(begin+1, end):
        value = items[i][0]
        if depth == 1 and value == 'source_details' and items[i+1][0] == '{':
            sources.append((i+1, closing(items, i+1)))
        depth += (value == '{') - (value == '}')
    require(len(sources) == 1, 'Edição exige um único source_details estático.')
    begin, end = sources[0]
    expected = {'boot_volume_size_in_gbs':'50', 'boot_volume_vpus_per_gb':'10'}
    found = set()
    edits = []
    depth = 1
    for i in range(begin+1, end):
        value = items[i][0]
        if depth == 1 and value in expected and items[i+1][0] == '=':
            require(value not in found, 'Atributo de disco duplicado.')
            found.add(value)
            first = i+2
            line_end = text.find('\n', items[first][1])
            if line_end < 0: line_end = len(text)
            last = first
            while last+1 < end and items[last+1][1] < line_end:
                last += 1
            expression = text[items[first][1]:items[last][2]]
            require(bool(re.fullmatch(r'(?:[0-9]+|"[0-9]+"|null|var\.[A-Za-z_][A-Za-z0-9_-]*)', expression.strip())),
                    'Expressão de disco complexa: revisão necessária antes de editar.')
            edits.append((items[first][1], items[last][2], expected[value]))
        depth += (value == '{') - (value == '}')
    missing = ''.join(f'\n    {key} = {value}' for key, value in expected.items() if key not in found)
    if missing:
        edits.append((items[end][1], items[end][1], missing + '\n  '))
    for start, finish, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[finish:]
    info, _ = entries[entry_index]
    entries[entry_index] = (info, (b'\xef\xbb\xbf' if bom else b'') + text.encode('utf-8'))
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        for info, content in entries:
            archive.writestr(info, content)
    return output.getvalue()
