import asyncio
import json
import sys
from playwright.async_api import async_playwright

async def main():
    """
    A standalone script to test Playwright browser configuration and page content.
    - Takes a URL as a command-line argument.
    - Loads browser context options from config.json.
    - Launches a browser with anti-bot-detection flags.
    - Navigates to the URL and saves a dump of the page's HTML.
    """
    # 1. Get URL from command-line arguments
    if len(sys.argv) < 2:
        print("Usage: python test_browser.py <URL>")
        sys.exit(1)
    
    target_url = sys.argv[1]
    output_filename = "test_dump.html"

    # 2. Load browser context options from config.json
    browser_context_options = {}
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
        browser_context_options = config.get('browser_context_options', {})
        print(f"Loaded browser context options: {browser_context_options}")
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as e:
        print(f"Warning: Could not load browser context options from config.json: {e}. Using defaults.")

    # 3. Launch browser with stealth arguments
    async with async_playwright() as p:
        print("Launching browser with anti-bot-detection arguments...")
        browser = await p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-infobars',
                '--window-position=0,0',
                '--ignore-certificate-errors',
                '--ignore-certificate-errors-spki-list',
            ]
        )
        
        context = await browser.new_context(**browser_context_options)
        page = await context.new_page()

        # 4. Navigate and dump page
        try:
            print(f"Navigating to {target_url}...")
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            
            print("Waiting for 5 seconds for dynamic content to load...")
            await page.wait_for_timeout(5000)
            
            print("Dumping page content...")
            html_content = await page.content()
            
            with open(output_filename, "w", encoding="utf-8") as f:
                f.write(html_content)
            
            print(f"Successfully saved page HTML to {output_filename}")

        except Exception as e:
            print(f"An error occurred: {e}")
        finally:
            print("Closing browser...")
            await browser.close()

if __name__ == "__main__":
    asyncio.run(main())
