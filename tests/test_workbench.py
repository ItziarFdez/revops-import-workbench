import csv
import json
import tempfile
import unittest
from pathlib import Path

import workbench


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'input.csv'
        self.config = self.root / 'mapping.json'
        self.mapping = {'columns': [
            {'source': 'Email', 'target': 'Email', 'type': 'email', 'required': True, 'normalize': ['trim', 'lower']},
            {'source': 'Code', 'target': 'External_ID__c', 'type': 'text'}], 'unique_key': ['Email']}

    def run_import(self, rows, mapping=None):
        with self.source.open('w', newline='') as handle:
            csv.writer(handle).writerows(rows)
        self.config.write_text(json.dumps(mapping or self.mapping))
        return workbench.transform(self.source, self.config, self.root / 'output')

    def test_normalized_deduplication_and_leading_zero_preservation(self):
        report = self.run_import([['Email', 'Code'], [' HELLO@EXAMPLE.COM ', '00001'], ['hello@example.com', '00002']])
        self.assertEqual(report['accepted_rows'], 1)
        self.assertEqual(report['error_counts'], {'duplicate_key': 1})
        with (self.root / 'output/accepted.csv').open() as handle:
            self.assertEqual(list(csv.reader(handle))[1], ['hello@example.com', '00001'])

    def test_invalid_record_does_not_consume_duplicate_key(self):
        self.mapping['columns'][1]['required'] = True
        report = self.run_import([['Email', 'Code'], ['a@example.com', ''], ['a@example.com', '001']])
        self.assertEqual(report['accepted_rows'], 1)
        self.assertEqual(report['error_counts'], {'required': 1})

    def test_malformed_records_keep_all_original_values(self):
        report = self.run_import([['Email', 'Code'], ['a@example.com', '001', 'extra'], ['b@example.com']])
        self.assertEqual(report['rejected_rows'], 2)
        with (self.root / 'output/rejected.csv').open() as handle:
            rejected = list(csv.DictReader(handle))
        self.assertEqual(json.loads(rejected[0]['original_values_json']), ['a@example.com', '001', 'extra'])
        self.assertEqual(json.loads(rejected[1]['original_values_json']), ['b@example.com'])

    def test_formula_payload_is_quarantined_and_not_in_html(self):
        payload = '=HYPERLINK("https://example.invalid","click")'
        report = self.run_import([['Email', 'Code'], ['a@example.com', payload]])
        self.assertEqual(report['error_counts'], {'spreadsheet_formula_risk': 1})
        self.assertNotIn('HYPERLINK', (self.root / 'output/review.html').read_text())

    def test_negative_number_and_positive_phone_are_valid(self):
        self.assertEqual(workbench.convert('-250.50', {'type': 'number'}), '-250.50')
        self.assertEqual(workbench.convert('+34 600 123 456', {'type': 'phone'}), '+34 600 123 456')

    def test_invalid_dates_and_nonfinite_numbers_are_rejected(self):
        for value, kind in [('2026-02-30', 'date'), ('NaN', 'number'), ('Infinity', 'number'), ('1e999999', 'number')]:
            with self.subTest(value=value), self.assertRaises(workbench.ImportError):
                workbench.convert(value, {'type': kind})

    def test_source_is_not_modified_and_existing_output_is_not_overwritten(self):
        self.run_import([['Email', 'Code'], ['a@example.com', '0001']])
        before = self.source.read_bytes()
        output = self.root / 'output/accepted.csv'
        accepted = output.read_bytes()
        with self.assertRaises(workbench.ImportError):
            workbench.transform(self.source, self.config, self.root / 'output')
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(output.read_bytes(), accepted)

    def test_duplicate_headers_and_unmapped_columns_fail_explicitly(self):
        self.source.write_text('Email,Email\na,b\n')
        with self.assertRaises(workbench.ImportError):
            workbench.inspect(self.source)
        with self.assertRaises(workbench.ImportError):
            workbench.validate_mapping(self.mapping, ['Email', 'Code', 'PrivateNote'])

    def test_bom_semicolon_and_quoted_multiline_input(self):
        self.source.write_text('\ufeffEmail;Code\r\na@example.com;"two\nlines"\r\n', encoding='utf-8')
        self.mapping['delimiter'] = ';'
        self.config.write_text(json.dumps(self.mapping))
        report = workbench.transform(self.source, self.config, self.root / 'output')
        self.assertEqual(report['input_rows'], 1)
        self.assertEqual(report['accepted_rows'], 1)

    def test_report_excludes_input_values_and_runs_are_reproducible(self):
        first = self.run_import([['Email', 'Code'], ['private@example.com', '0001'], ['invalid', '0002']])
        second = workbench.transform(self.source, self.config, self.root / 'another-output')
        self.assertEqual(first, second)
        self.assertNotIn('private@example.com', (self.root / 'output/report.json').read_text())
        self.assertEqual((self.root / 'output/accepted.csv').read_bytes(),
                         (self.root / 'another-output/accepted.csv').read_bytes())


if __name__ == '__main__':
    unittest.main()
