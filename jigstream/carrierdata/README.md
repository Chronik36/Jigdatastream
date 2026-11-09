# Carrier tracking-number formats

`couriers.json` is the courier data from [jkeen/tracking_number_data](https://github.com/jkeen/tracking_number_data)
(MIT License, copyright Jeff Keen, see `LICENSE` here), commit 03af203 (2026-09-01), with the couriers merged into
one file and the glossary left out. `jigstream/tracknum.py` reads it.

To update: replace `couriers.json` with the newer data in the same shape and run `pytest tests/test_scraper_survey.py`.
