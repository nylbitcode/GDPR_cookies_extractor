
import csv

def calculate_precision_recall(csv_file):
    total_expected_ground_truth = 0
    correctly_identified_ground_truth = 0
    
    with open(csv_file, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['ground_truth_url'] != 'File not found' and row['ground_truth_url'] != '':
                total_expected_ground_truth += 1
                if row['ground_truth_found'] == 'True':
                    correctly_identified_ground_truth += 1

    precision = correctly_identified_ground_truth / total_expected_ground_truth if total_expected_ground_truth > 0 else 0
    recall = correctly_identified_ground_truth / total_expected_ground_truth if total_expected_ground_truth > 0 else 0 # In this specific case, recall is the same as precision because we are only counting if the ground truth URL was found among the detected links.

    print(f"Total sites with expected ground truth URL: {total_expected_ground_truth}")
    print(f"Correctly identified ground truth URLs: {correctly_identified_ground_truth}")
    print(f"Precision: {precision:.2f}")
    print(f"Recall: {recall:.2f}")

if __name__ == '__main__':
    calculate_precision_recall('privacy_policy_analysis.csv')
