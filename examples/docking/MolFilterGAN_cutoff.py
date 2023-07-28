import argparse
import csv

parser = argparse.ArgumentParser()
parser.add_argument('--infile_path', type=str,help='output from MolFilterGAN')
parser.add_argument('--cut_off', type=float,help='MolFilterGAN percent cutoff')
args = parser.parse_args()
with open('filtered.smi', mode = 'w') as out:
    with open(args.infile_path, mode ='r') as file:
        csvFile = csv.reader(file)
        for lines in csvFile:
            if 'logits' not in lines:
                smi = lines[0]
                score = float(lines[1])
                if score >= args.cut_off:
                    out.write(smi+'\n')
