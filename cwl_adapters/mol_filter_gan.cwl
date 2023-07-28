#!/usr/bin/env cwl-runner
cwlVersion: v1.0

class: CommandLineTool

label: MolFilterGAN tool for virtual screening

# Note for some reason cp command has issues with glob patterns * in cwltool but rsync works fine
baseCommand: ["conda", "run", "-n", "MolFilterGAN", "python", "/MolFilterGAN/Prediction.py"]

hints:
  cwltool:CUDARequirement:
    cudaVersionMin: "11.7"
    cudaComputeCapabilityMin: "3.0"
    cudaDeviceCount: 0
  DockerRequirement:
    dockerPull: mrbrandonwalker/mol-filter-gan:mol-filter-gan

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

  load_dir:
    type: string
    inputBinding:
      position: 2
      separate: true
      prefix: --load_dir
    default: "/MolFilterGAN/ADtrained_D.ckpt"


  voc_path:
    type: string
    inputBinding:
      position: 3
      separate: true
      prefix: --voc_path
    default: "/MolFilterGAN/Datasets/Voc"
 
outputs:

  stdout:
    type: File
    outputBinding:
      glob: stdout

  output_scores:
    type: File
    outputBinding:
      glob: "*smi_out.csv"

stdout: stdout

$namespaces:
  edam: https://edamontology.org/
  cwltool: http://commonwl.org/cwltool#

$schemas:
- https://raw.githubusercontent.com/edamontology/edamontology/master/EDAM_dev.owl
