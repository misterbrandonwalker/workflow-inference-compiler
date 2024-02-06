#!/usr/bin/env cwl-runner
cwlVersion: v1.2

class: CommandLineTool

label: Flatten array of array

doc: |-
  Flatten array of array

baseCommand: 'true'

requirements:
  InlineJavascriptRequirement: {}

inputs:

  input_array:
    type:
        type: array
        items:
            type: array
            items: File

  output_array:
    type: string?

outputs:

  output_array:
    type: Any[]
    outputBinding:
      glob:
      outputEval: $(inputs.input_array.map(i => i.map(f => f.path)).flat())

$namespaces:
  edam: https://edamontology.org/

$schemas:
- https://raw.githubusercontent.com/edamontology/edamontology/master/EDAM_dev.owl