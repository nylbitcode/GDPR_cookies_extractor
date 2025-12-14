# GDPR Cookie & Privacy Policy Analyzer

This tool automates the analysis of websites for GDPR compliance regarding cookie consent and privacy policies. It uses Playwright for browser automation and a Large Language Model (LLM) via Ollama for semantic analysis of privacy texts.

## Features

- **Task-Based Execution**: Run specific analysis tasks independently (e.g., only scrape cookies, or only find the DPO).
- **Automatic Dependency Management**: If a task requires a prerequisite (e.g., finding the DPO requires the privacy policy page), the script runs the prerequisite task automatically.
- **Incremental Analysis & Resuming**: Results are saved per-site. The script automatically detects completed tasks and only runs what's necessary, allowing you to stop and resume analysis at any time.
- **Multi-scenario Cookie Analysis**: Tests website behavior for different consent choices (e.g., initial visit, accepting all).
- **Cookie Categorization**: Uses an LLM to categorize captured cookies into classes like "Strictly Necessary," "Analytical," "Marketing," etc.
- **Privacy Policy Analysis**: Scans privacy policy pages to find information about Data Protection Officer (DPO), data retention, and data deletion procedures.

## Setup

### Clone the Repository
```bash
git clone <your-repository-url>
cd GDPR_cookies_extractor
```

### 1. Install

To run the script directly on your machine:

1.  Install the required Python dependencies using Poetry:
    ```bash
    poetry install
    ```
2.  Install Playwright browsers and their system dependencies:
    ```bash
    poetry run playwright install --with-deps
    ```

### Configuration
- **`config.json`**: This file contains the main configuration.
  - `llm`: Set the model for Ollama (e.g., `llama3`).
  - `browser_context_options`: Configure the browser's locale, timezone, user agent, etc.
  - `search_keywords`: Add keywords to help the script find cookie banners and policy links.
- **`sites.csv`**: For batch analysis, add a list of URLs. The file should have a header `website_url` or contain a single column of URLs.
  ```csv
  website_url
  https://example.com
  https://anothersite.org
  ```

## Output Structure

The script creates a dedicated folder for each analyzed site, centralizing all its results.

- **`output/{site_name}/`**:
  - **`results.json`**: A JSON file containing the structured results for all analysis scenarios run on this site.
  - **`dumps/`**: A subfolder containing detailed dumps for each analysis phase, including the full HTML (`.html`) and extracted links (`_links.json`).

## Usage

The script is now task-driven. You specify which site(s) to analyze and which task(s) to perform.

### Command Structure
```bash
poetry run main <input_option> --tasks <task_1> <task_2> ...
```

### Input Options (Choose one)
- `--url <url>`: Analyze a single URL.
- `--file [path/to/file.csv]`: Analyze all URLs in a CSV file. If no path is given, it defaults to `sites.csv`.

### Task Options (`--tasks`)
This flag specifies which analysis tasks to run. If not provided, it defaults to `all`.

| Task           | Description                                                                                             |
|----------------|---------------------------------------------------------------------------------------------------------|
| `all`          | **(Default)** Runs all available analysis tasks.                                                        |
| `scrape`       | Scrapes and categorizes cookies for each defined scenario (initial, accept, etc.).                      |
| `find-pp`      | Finds the URL of the main privacy policy page.                                                          |
| `analyze-pp`   | A meta-task that runs all sub-analyses on the privacy policy (`find-cd`, `find-dpo`, etc.).             |
| `find-cd`      | Finds the Cookie Declaration page. (Depends on `find-pp`)
| `find-dpo`     | Finds contact information for the Data Protection Officer. (Depends on `find-pp`)                         |
| `find-delete`  | Finds information on data deletion procedures. (Depends on `find-pp`)
| `find-retention`| Finds information on data retention periods. (Depends on `find-pp`)                                     |

### Examples

**Analyze a single site with all tasks:**
```bash
poetry run main --url https://www.example.com --tasks all
```

**Analyze all sites in `sites.csv`, but only scrape cookies:**
```bash
poetry run main --file sites.csv --tasks scrape
```

**Find the privacy policy for all sites:**
```bash
poetry run main --file sites.csv --tasks find-pp
```

**After finding the privacy policy, find the DPO information:**
(The script will load the previously found policy URL and use it)
```bash
poetry run main --file sites.csv --tasks find-dpo
```

**Run both scraping and all privacy policy analyses:**
```bash
poetry run main --file sites.csv --tasks scrape analyze-pp
```

### Docker Execution
This is the recommended way to run the analysis for consistent results.

**1. Build the Docker Image**
```bash
docker build -t gdpr_extractor .
```

**2. Run the Container**
Mount local `output` and `logs` directories to access the results.

**To analyze sites from `sites.csv` with all tasks:**
```bash
docker run -it --rm \
 -v ./output:/app/output \
 -v ./logs:/app/logs \
 gdpr_extractor \
 poetry run main --file sites.csv --tasks all
```
*(Add `--gpus all` after `docker run` if you have a compatible GPU to accelerate LLM work.)*

**To analyze a single site for a specific task (e.g., `scrape`):**
```bash
docker run -it --rm \
 -v ./output:/app/output \
 -v ./logs:/app/logs \
 gdpr_extractor \
 poetry run main --url https://www.example.com --tasks scrape
```