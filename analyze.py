
import json
import os
import csv

def analyze_privacy_policy_links():
    results = []
    summary_file = 'output_summary_cleaned.json'
    if not os.path.exists(summary_file):
        print(f"Error: {summary_file} not found.")
        return

    with open(summary_file, 'r') as f:
        summary_data = json.load(f)

    sites = summary_data.keys()

    for site in sites:
        links_file = os.path.join('FINAL RES/output', site, 'dumps', 'find_privacy_policy_hop_0_links.json')
        results_file = os.path.join('FINAL RES/output', site, 'results.json')
        pp_label = summary_data.get(site, {}).get('PP', {}).get('label', 'N/A')

        total_detected_links = 'File not found'
        detected_links = []
        if os.path.exists(links_file):
            with open(links_file, 'r') as f:
                try:
                    detected_links_data = json.load(f)
                    if isinstance(detected_links_data, list):
                        detected_links = [item['href'].rstrip('/') for item in detected_links_data if 'href' in item]
                        total_detected_links = len(detected_links)
                    else:
                        total_detected_links = 0
                except json.JSONDecodeError:
                    total_detected_links = 0
        
        ground_truth_url = 'File not found'
        if os.path.exists(results_file):
            with open(results_file, 'r') as f:
                results_data = json.load(f)
                if isinstance(results_data, list):
                    for scenario in results_data:
                        if scenario.get('scenario') == 'initial':
                            raw_url = scenario.get('privacy_policy_url')
                            if raw_url:
                                ground_truth_url = raw_url.rstrip('/')
                            else:
                                ground_truth_url = ''
                            break
                elif isinstance(results_data, dict):
                    raw_url = results_data.get('initial', {}).get('privacy_policy_url')
                    if raw_url:
                        ground_truth_url = raw_url.rstrip('/')
                    else:
                        ground_truth_url = ''

        url_found = 'F'
        if ground_truth_url and ground_truth_url != 'File not found':
            if ground_truth_url in detected_links:
                url_found = 'T'

        results.append({
            'website': site,
            'total_detected_links': total_detected_links,
            'privacy_policy_url': ground_truth_url,
            'pp_label': pp_label,
            'url_found': url_found
        })

    output_file = 'privacy_policy_analysis.csv'
    with open(output_file, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['website', 'total_detected_links', 'privacy_policy_url', 'pp_label', 'url_found'])
        writer.writeheader()
        writer.writerows(results)

    print(f"Analysis complete. Results saved to {output_file}")

if __name__ == '__main__':
    analyze_privacy_policy_links()
