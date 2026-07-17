---
dataset_info:
  features:
  - name: id
    dtype: string
  - name: nl_statement
    dtype: string
  - name: nl_proof
    dtype: string
  - name: lean4_src_header
    dtype: string
  - name: lean4_formalization
    dtype: string
  splits:
  - name: valid
    num_bytes: 225857.5741239892
    num_examples: 185
  - name: test
    num_bytes: 227078.4258760108
    num_examples: 186
  download_size: 208205
  dataset_size: 452936.0
configs:
- config_name: default
  data_files:
  - split: valid
    path: data/valid-*
  - split: test
    path: data/test-*
license: mit
task_categories:
- translation
- text-generation
language:
- en
tags:
- code
pretty_name: ProofNet#
size_categories:
- n<1K
---

# ProofNet#

ProofNet# is a Lean 4 port of the [ProofNet](https://huggingface.co/datasets/hoskinson-center/proofnet) benchmark including fixes.
A comparison with previous Lean 4 ports can be found at:
https://proofnet4-fix.streamlit.app/.

This benchmark is compatible with all Lean versions between v4.7.0 and v4.16.0-rc2.

### Original Dataset Summary

ProofNet is a benchmark for autoformalization and formal proving of undergraduate-level mathematics. The ProofNet benchmarks consists of 371 examples, each consisting of a formal theorem statement in Lean 3, a natural language theorem statement, and a natural language proof. The problems are primarily drawn from popular undergraduate pure mathematics textbooks and cover topics such as real and complex analysis, linear algebra, abstract algebra, and topology. We intend for ProofNet to be a challenging benchmark that will drive progress in autoformalization and automatic theorem proving.

## Tasks

- **Statement Autoformalization:**
  - Input: `nl_statement`
  - Output: `lean4_formalization`
- **Proof Autoformalization:**
  - Input: `lean4_formalization`, `nl_proof`, 
  - Output: use Lean to check if the generated proof is correct.
- **Theorem Proving:**
  - Input: `lean4_formalization`
  - Output: use Lean to check if the generated proof is correct.

## Data Fields

- `id`: Unique string identifier for the problem.
- `nl_statement`: Natural language theorem statement.
- `nl_proof`: Mathematical proof in natural language for the theorem statement.
- `lean4_src_header`: File header including imports, namespaces, and locales required for the formal statement.
- `lean4_formalization`: Formal theorem statement in Lean 4.
    
## Citation

ProofNet# is introduced in [Improving Autoformalization using Type Checking](https://arxiv.org/abs/2406.07222).
```bibtex
@misc{poiroux2024improvingautoformalizationusingtype,
    title={Improving Autoformalization using Type Checking}, 
    author={Auguste Poiroux and Gail Weiss and Viktor Kunčak and Antoine Bosselut},
    year={2024},
    eprint={2406.07222},
    archivePrefix={arXiv},
    primaryClass={cs.CL},
    url={https://arxiv.org/abs/2406.07222}, 
}
```

Original work where ProofNet has been introduced:
```bibtex
@misc{azerbayev2023proofnet,
      title={ProofNet: Autoformalizing and Formally Proving Undergraduate-Level Mathematics}, 
      author={Zhangir Azerbayev and Bartosz Piotrowski and Hailey Schoelkopf and Edward W. Ayers and Dragomir Radev and Jeremy Avigad},
      year={2023},
      eprint={2302.12433},
      archivePrefix={arXiv},
      primaryClass={cs.CL}
}
```