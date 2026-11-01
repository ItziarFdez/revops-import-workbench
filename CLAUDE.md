# Mapping assistant workflow

Read README.md and workbench.py before drafting a mapping.

1. Ask for the intended target CRM and approved field API names. Do not guess target fields or consent semantics.
2. Prefer source headers and type descriptions over real customer records. Do not open or transmit private row values unless the operator explicitly authorizes that use.
3. Draft a mapping in private/mapping.json. Explain required fields, normalization, duplicates, and unmapped columns; do not discard columns silently.
4. Use `workbench.py inspect` to confirm exact source headers when permitted. Keep row values out of chat and reports.
5. Show the proposed mapping for review. Once approved, run the transform into a new output directory and report aggregate counts and issue types.
6. Do not import records into Salesforce or another service. This project prepares files only. Treat rejected.csv as sensitive and potentially unsafe spreadsheet content.
7. Do not describe this deterministic utility as an AI agent or claim a successful CRM import without independent evidence.
