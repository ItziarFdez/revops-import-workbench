#!/usr/bin/env python3
"""Local CSV mapping and validation for CRM imports. Python standard library only."""
import argparse
import csv
import hashlib
import html
import io
import json
import re
import shutil
import sys
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

KINDS = {'text', 'email', 'number', 'date', 'boolean', 'phone'}
OPERATIONS = {'trim', 'lower', 'upper'}


class ImportError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ImportError(message)


def read_csv(path, delimiter=','):
    require(delimiter in {',', ';', '\t', '|'}, 'Unsupported delimiter')
    raw = Path(path).read_bytes()
    try:
        content = raw.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ImportError('Input must be UTF-8. Convert its encoding before processing.') from exc
    require('\x00' not in content, 'Input contains null bytes')
    reader = csv.reader(io.StringIO(content, newline=''), delimiter=delimiter, strict=True)
    header = next(reader, None)
    require(header is not None and len(header) > 0, 'CSV is empty')
    require(all(h.strip() for h in header), 'Empty column names are not supported')
    require(len(set(header)) == len(header), 'Duplicate CSV headers; rename them before mapping')
    rows = []
    # Row identifiers count logical CSV records, not physical lines; quoted
    # multiline fields remain a single record.
    for record_number, values in enumerate(reader, 2):
        rows.append((record_number, values))
    return raw, header, rows


def inspect(path, delimiter=','):
    raw, header, rows = read_csv(path, delimiter)
    return {'columns': header, 'data_rows': len(rows),
            'malformed_rows': sum(len(values) != len(header) for _, values in rows),
            'sha256': hashlib.sha256(raw).hexdigest(),
            'note': 'No row values are included. Column names may still be confidential.'}


def validate_mapping(mapping, headers):
    require(isinstance(mapping, dict) and set(mapping) <= {'columns', 'unique_key', 'unmapped_columns', 'delimiter'},
            'Unknown mapping keys')
    columns = mapping.get('columns')
    require(isinstance(columns, list) and len(columns) > 0, 'Provide at least one column mapping')
    targets, sources = set(), set()
    for column in columns:
        require(isinstance(column, dict) and set(column) <= {'source', 'target', 'type', 'required', 'normalize'},
                'Unsupported column mapping keys')
        source, target = column.get('source'), column.get('target')
        require(isinstance(source, str) and source in headers, 'Mapping source does not match a CSV header')
        require(isinstance(target, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', target) is not None,
                'Target must be a simple CRM field API name')
        require(target.casefold() not in targets, 'Duplicate target field')
        targets.add(target.casefold())
        sources.add(source)
        require(column.get('type', 'text') in KINDS, 'Unsupported column type')
        require(type(column.get('required', False)) is bool, 'required must be true or false')
        operations = column.get('normalize', [])
        require(isinstance(operations, list) and all(isinstance(op, str) and op in OPERATIONS for op in operations),
                'Supported normalization operations: trim, lower, upper')
    key = mapping.get('unique_key', [])
    require(isinstance(key, list) and len(set(key)) == len(key), 'unique_key must be a list of distinct target fields')
    require(all(k in [c['target'] for c in columns] for k in key), 'Unknown unique_key target field')
    require(mapping.get('unmapped_columns', 'reject') in {'reject', 'ignore'}, 'Invalid unmapped_columns policy')
    unmapped = sorted(set(headers) - sources)
    require(not unmapped or mapping.get('unmapped_columns') == 'ignore',
            'Unmapped input columns: ' + ', '.join(unmapped) + '. Map them or explicitly choose ignore.')
    return unmapped


def convert(value, column):
    for operation in column.get('normalize', []):
        value = {'trim': str.strip, 'lower': str.lower, 'upper': str.upper}[operation](value)
    if value == '':
        require(not column.get('required', False), 'required')
        return value
    require(not any(ord(c) < 32 and c not in '\n\r\t' for c in value), 'control_character')
    kind = column.get('type', 'text')
    if kind == 'number':
        try:
            number = Decimal(value)
            require(number.is_finite(), 'invalid_number')
            require(len(value) <= 100 and abs(number.adjusted()) <= 100, 'number_out_of_range')
            value = format(number, 'f')
        except InvalidOperation as exc:
            raise ImportError('invalid_number') from exc
    elif kind == 'date':
        require(re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) is not None, 'invalid_date')
        try:
            value = date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise ImportError('invalid_date') from exc
    elif kind == 'email':
        require(re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value) is not None, 'invalid_email')
    elif kind == 'boolean':
        require(value.casefold() in {'true', 'false', 'yes', 'no', '1', '0'}, 'invalid_boolean')
        value = 'true' if value.casefold() in {'true', 'yes', '1'} else 'false'
    elif kind == 'phone':
        require(re.fullmatch(r'\+?[0-9() .-]{3,30}', value) is not None, 'invalid_phone')
    # Preserve business values rather than prefixing apostrophes. Quarantine
    # potentially executable spreadsheet text, except validated numbers/phones.
    if kind not in {'number', 'phone'}:
        require(not value.lstrip().startswith(('=', '+', '-', '@')), 'spreadsheet_formula_risk')
    return value


def write_csv(path, header, rows):
    with Path(path).open('w', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def transform(source, mapping_path, output):
    mapping_bytes = Path(mapping_path).read_bytes()
    mapping = json.loads(mapping_bytes.decode('utf-8'))
    require(isinstance(mapping, dict), 'Mapping must be a JSON object')
    raw, headers, rows = read_csv(source, mapping.get('delimiter', ','))
    unmapped = validate_mapping(mapping, headers)
    output = Path(output)
    require(not output.exists(), 'Output directory already exists; use a new directory')
    columns = mapping['columns']
    indices = {name: i for i, name in enumerate(headers)}
    targets = [c['target'] for c in columns]
    accepted, rejected, errors = [], [], []
    seen = set()
    counts = Counter()
    for number, values in rows:
        row_errors, converted = [], []
        if len(values) != len(headers):
            row_errors.append({'field': None, 'code': 'column_count'})
        else:
            for column in columns:
                try:
                    converted.append(convert(values[indices[column['source']]], column))
                except ImportError as exc:
                    row_errors.append({'field': column['target'], 'code': str(exc)})
                    converted.append('')
            if not row_errors and mapping.get('unique_key'):
                key = tuple(converted[targets.index(k)] for k in mapping['unique_key'])
                if any(v == '' for v in key):
                    row_errors.append({'field': None, 'code': 'missing_unique_key'})
                elif key in seen:
                    row_errors.append({'field': None, 'code': 'duplicate_key'})
                else:
                    seen.add(key)
        if row_errors:
            # Original input is preserved as JSON in a fixed-width CSV row,
            # including malformed records with extra or missing cells.
            codes = ';'.join((e['field'] + ':' if e['field'] else '') + e['code'] for e in row_errors)
            rejected.append([str(number), json.dumps(values, ensure_ascii=False), codes])
            errors.append({'record': number, 'errors': row_errors})
            counts.update(e['code'] for e in row_errors)
        else:
            accepted.append(converted)
    report = {'input_rows': len(rows), 'accepted_rows': len(accepted), 'rejected_rows': len(rejected),
              'source_sha256': hashlib.sha256(raw).hexdigest(),
              'mapping_sha256': hashlib.sha256(mapping_bytes).hexdigest(),
              'ignored_columns': unmapped, 'error_counts': dict(sorted(counts.items())),
              'duplicate_policy': 'First valid record wins; later duplicates are rejected.',
              'normalization': 'Only explicitly configured operations are applied.',
              'errors': errors}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Create privately. Never overwrite another run or publish sensitive outputs.
    output.mkdir(mode=0o700)
    try:
        write_csv(output / 'accepted.csv', targets, accepted)
        write_csv(output / 'rejected.csv', ['source_record', 'original_values_json', 'reasons'], rejected)
        (output / 'report.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
        cards = ''.join('<div class="card"><strong>' + str(report[key]) + '</strong><span>' + label + '</span></div>'
                        for key, label in [('input_rows', 'Input records'), ('accepted_rows', 'Ready for import'),
                                           ('rejected_rows', 'Need review')])
        reasons = ''.join('<tr><td>' + html.escape(code) + '</td><td>' + str(count) + '</td></tr>'
                          for code, count in sorted(counts.items()))
        page = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">' + \
            '<title>RevOps Import Review</title><style>body{font:16px system-ui;background:#f4f6fa;color:#16233b;' + \
            'max-width:960px;margin:64px auto;padding:24px}h1{font-size:40px;margin-bottom:8px}.cards{display:flex;gap:16px;' + \
            'flex-wrap:wrap;margin:32px 0}.card{background:white;padding:24px;border-radius:14px;flex:1;min-width:140px}' + \
            '.card strong{display:block;font-size:40px;color:#166d66}.card span{color:#526079}table{width:100%;border-collapse:collapse;' + \
            'background:white}td,th{text-align:left;padding:14px;border-bottom:1px solid #e0e5ed}code{word-break:break-all}' + \
            '</style><main><p>DATA QUALITY / REVIEW BEFORE IMPORT</p><h1>RevOps Import Review</h1>' + \
            '<p>A local validation report. No records have been sent to your CRM.</p><div class="cards">' + cards + \
            '</div><h2>Issues to resolve</h2><table><tr><th>Validation issue</th><th>Occurrences</th></tr>' + \
            (reasons or '<tr><td>No validation issues</td><td>0</td></tr>') + '</table>' + \
            '<h2>Next steps</h2><ol><li>Review rejected.csv as text: it contains original, potentially unsafe values.</li>' + \
            '<li>Check mappings and source records; run again into a new output directory.</li>' + \
            '<li>Test accepted.csv in an authorized sandbox before importing elsewhere.</li></ol>' + \
            '<p>Duplicate policy: first valid record wins. This tool does not determine which real-world record is authoritative.</p>' + \
            '<p>Source fingerprint: <code>' + report['source_sha256'] + '</code></p></main></html>'
        (output / 'review.html').write_text(page)
    except BaseException:
        shutil.rmtree(output)
        raise
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    i = sub.add_parser('inspect', help='List headers and counts without displaying row values')
    i.add_argument('source')
    i.add_argument('--delimiter', default=',')
    t = sub.add_parser('transform', help='Map and validate a CSV without contacting any service')
    t.add_argument('source')
    t.add_argument('--mapping', required=True)
    t.add_argument('--out', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'inspect':
            report = inspect(args.source, args.delimiter)
        else:
            report = transform(args.source, args.mapping, args.out)
        print(json.dumps({k: v for k, v in report.items() if k != 'errors'}, indent=2))
        return 1 if report.get('rejected_rows', 0) else 0
    except (ImportError, OSError, ValueError, TypeError, KeyError, csv.Error) as exc:
        print('Error: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
