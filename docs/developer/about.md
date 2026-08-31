# About

## The project and the lab

HybridInference is developed by the
[Harvard MadSys Lab](https://juncheng.seas.harvard.edu/) at the Harvard John A.
Paulson School of Engineering and Applied Sciences, and published under the
[MIT license](https://github.com/HarvardMadSys/hybridInference/blob/main/LICENSE).
The routing system at its center, RouteWise, is the subject of the lab's
EuroSys '27 paper and ships separately as the MIT-licensed
[`llm-routewise`](https://github.com/HarvardMadSys/RouteWise) library; this
gateway is its production reference integration.

The people behind the project are visible where the work happens: the
[contributors page](https://github.com/HarvardMadSys/hybridInference/graphs/contributors)
lists everyone who has landed a change.

## How to cite

If you use HybridInference or RouteWise in your research, cite the RouteWise
paper:

```bibtex
@inproceedings{tian2027routewise,
  title     = {{RouteWise}: Latency--Cost Optimization for Multi-Provider LLM Routing},
  author    = {Muxin Tian and Haoran Ni and Yiyan Zhai and Yangsun Park and Juncheng Yang},
  booktitle = {Proceedings of the 22nd European Conference on Computer Systems (EuroSys '27)},
  year      = {2027}
}
```

## See it running

[FreeInference](https://freeinference.org/) is the lab's own deployment of
this gateway. A deployment's identity — branding, team, sponsors, user-facing
documentation — lives with the deployment, in its overlay and on its own
site, not in this repository. That separation is why these pages name no
default provider and no team: a fresh clone comes up as your gateway, not
ours.

## Getting in touch

Bugs and questions go to the
[issue tracker](https://github.com/HarvardMadSys/hybridInference/issues);
patches follow the [contributing guide](contributing.md).
