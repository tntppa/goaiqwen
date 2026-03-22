# Changelog

All notable updates to this project are documented in this file.

## 2026-03-21 v9
- Added `config/generation_config.py` for centralized model generation settings.
- Configured the model for faster and more deterministic output for extraction, OCR, and structured parsing tasks.
- Updated the backend to load generation parameters from the config file and apply them to Ollama requests.

## 2026-03-19 v8
- Added support for uploading multiple files in one request.
- Added mixed-format analysis for images, PDF, Excel, and Word files.
- Updated the frontend with multi-file selection, drag-and-drop, file count, and grid preview.
- Updated the backend to aggregate all images and Excel text, then call the model once per request.

## 2026-03-19 v7
- Moved prompts out of `app.py` into the `prompts/` directory.
- Switched prompt management to Markdown files.
- Added versioned prompts with `v5` and `v6` for easier comparison and iteration.
- Updated the backend to load prompt templates dynamically.

## 2026-03-19 v6
- Split frontend code into `templates/` and `static/` directories.
- Switched the homepage to `render_template("index.html")`.
- Separated page styles and scripts for easier maintenance.
- Added response text sanitization to avoid Unicode surrogate character errors.

## 2026-03-19 v5
- Upgraded the prompt strategy for multi-document consolidation.
- Added cross-document context correlation and validation.
- Merged item information across documents into a unified output.
- Added automatic conflict reporting in `error_check_notes`.

## 2026-03-17 v4
- Added support for converting DOC and DOCX files into images for recognition.
- Improved prompt handling for different document types.

## 2026-03-17 v3
- Added elapsed time display for analysis results.
- Added the changelog panel on the right side of the page.

## 2026-03-17 v2
- Added XLS and XLSX upload support.
- Added Excel parsing with `pandas` before sending content to the AI model.
- Fixed the missing `pdftoppm` dependency issue.

## 2026-03-17 Initial Release
- Added JPG and PNG image recognition.
- Added PDF-to-image recognition.
- Added structured JSON output for customs declaration extraction.
- Integrated `Ollama` with `qwen2.5vl:7b`.
