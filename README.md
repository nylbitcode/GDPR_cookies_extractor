# GDPR Cookie & Privacy Policy Analyzer

This tool automates the analysis of websites for GDPR compliance regarding cookie consent and privacy policies. It uses Playwright for browser automation and a Large Language Model (LLM) via Ollama for semantic analysis of privacy texts.

## Features

- **Multi-scenario Analysis**: Tests website behavior for different consent choices (e.g., accepting, rejecting cookies).
- **Cookie Categorization**: Uses an LLM to categorize captured cookies into classes like "Strictly Necessary," "Analytical," "Marketing," etc.
- **Privacy Policy Analysis**: Scans privacy policy pages to find information about Data Protection Officer (DPO), data retention, and data deletion procedures.

## Setup

### Clone the Repository

```bash
git clone <your-repository-url>
cd GDPR_cookies_extractor
```

### Configuration

- **`config.json`**: This file contains the main configuration.
  - `llm`: Set the model for Ollama (e.g., `llama3`).
  - `browser_context_options`: Configure the browser's locale, timezone, user agent, and viewport to simulate a real user. The anti-bot detection launch arguments are hardcoded in `main.py`.
  - `scraper.cookie_banners`: Add base keywords (in English, Italian, or other languages) to help the script find the cookie consent buttons.
- **`sites.csv`**: For batch analysis, add a list of URLs to this file, one per line.

## Output Structure

- **`output/analysis_results_[timestamp].json`**: The main output file containing the structured results for all analyzed sites and scenarios.
- **`output/dumps/`**: Contains detailed dumps for each analysis phase, including the full HTML (`.html`) and all extracted links (`_links.json`).
- **`logs/`**: Contains the log files for the application's execution and the Ollama server.

### Local

#### 1. Install

To run the script directly on your machine:

1.  Install the required Python dependencies using Poetry:
    ```bash
    poetry install
    ```
2.  Install Playwright browsers and their system dependencies:
    ```bash
    poetry run playwright install --with-deps
    ```

#### 2. Execution

##### Analyze a Single Site

```bash
poetry run main https://www.example.com
```

##### Analyze All Sites from `sites.csv`

```bash
poetry run main
```

### Docker Execution

This is the recommended way to run the analysis for consistent results.

#### 1. Build the Docker Image

From the project's root directory, run:

```bash
docker build -t gdpr_extractor .
```

#### 2. Run the Container

The container will automatically create and use `output` and `logs` directories. To access these files on your host machine, you need to mount local folders using volumes.

First, create the local directories:

```bash
mkdir -p output logs
```

Then, run the container:

**To analyze sites from `sites.csv`:**

```bash
docker run -it --rm \
 -v ./output:/app/output \
 -v ./logs:/app/logs \
 gdpr_extractor
```

_(Add `--gpus all` after `docker run` if you have a compatible NVIDIA GPU and want to accelerate Ollama.)_

**To analyze a single site:**

```bash
docker run -it --rm \
 -v ./output:/app/output \
 -v ./logs:/app/logs \
 gdpr_extractor \
 poetry run main https://www.example.com
```
