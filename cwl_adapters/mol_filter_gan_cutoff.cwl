#!/usr/bin/env cwl-runner
cwlVersion: v1.0

class: CommandLineTool

label: score cutoff for MolFilterGAN tool

baseCommand: ["conda", "run", "python", "/MolFilterGAN_cutoff.py"]

hints:
  DockerRequirement:
    dockerPull: mrbrandonwalker/mol-filter-gan:mol-filter-gan-cutoff

requirements:
  InlineJavascriptRequirement: {}
  InitialWorkDirRequirement:
    listing: |
      ${
        var lst = [];
        var dict = {
            "entry": inputs.infile_path,
            "writable": true // Important!
          };
        lst.push(dict);
        return lst;
      }


inputs:

  infile_path:
    type: File
    inputBinding:
      position: 1
      separate: true
      prefix: --infile_path

  cut_off:
    type: float
    inputBinding:
      position: 2
      separate: true
      prefix: --cut_off
 
outputs:

  stdout:
    type: File
    outputBinding:
      glob: stdout

  filtered_ligands:
    type: File
    outputBinding:
      glob: "*filtered.smi"

stdout: stdout

$namespaces:
  edam: https://edamontology.org/
  cwltool: http://commonwl.org/cwltool#

$schemas:
- https://raw.githubusercontent.com/edamontology/edamontology/master/EDAM_dev.owl
