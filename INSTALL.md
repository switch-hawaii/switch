# Introduction

This repository contains code and example files for the Switch power system
planning model. Switch is written in the Python language and several other
open-source projects (notably Pyomo, Pandas and glpk). The instructions below
show you how to install  these components on a Linux, Mac or Windows computer.

This branch is a pre-release version of Switch used for the Hawaii price
response study. The instructions below will install this version on your
computer, which is useful for replicating that study. For new work, see
https://switch-model.org for advice on installing Switch.

We recommend that you use the Miniconda version of the Anaconda scientific
computing environment to install and run Switch. This provides an easy,
cross-platform way to install most of the resources that Switch needs, and it
avoids interfering with your system's built-in Python installation (if present).
The instructions below assume you will use Miniconda. If you prefer to use a
different distribution, you will need to adjust the instructions accordingly
(e.g., install Switch and its dependencies via pip.)


# Install Python

Download Miniconda from https://docs.conda.io/en/latest/miniconda.html and run
the installer.

# Install Switch and its Dependencies

We recommend creating a named conda environment specifically for this version of
Switch and its dependencies. This is necessary in particular on Macs with Apple
silicon, to create an x86-64 environment that is compatible with the version of
Pyomo used for this study.

After installing Miniconda, open an Anaconda Command Prompt (Windows) or
Terminal.app (Mac) and type the following commands (unless using Apple silicon):

    conda create --name price_response -c conda-forge python=3.7.3 switch_model=2.0.6 rpy2=3.1.0 scipy=1.3.1 pyomo=5.6.6 pyutilib=5.7.1

If running on a Mac with Apple silicon (M1 or M2 processor), you should use this
command instead:

    CONDA_SUBDIR=osx-64 conda create --name price_response -c conda-forge python=3.7.3 switch_model=2.0.6 rpy2=3.1.0 scipy=1.3.1 pyomo=5.6.6 pyutilib=5.7.1

This will install the official, released version of `switch`, along with various
software used by Switch (the Pyomo optimization framework, Pandas data
manipulation framework and glpk numerical solver) and extra packages used
for the price response study.

Next run the following commands:

    conda activate price_response
    pip install --ignore-installed --no-deps https://github.com/switch-hawaii/switch/archive/refs/heads/price_response_study.zip

These will install the exact version of Switch used for this study.

Note that whenever you open a new terminal or command window, you should always
run `conda activate price_response` to setup the environment correctly before
running any of the `switch` commands or the `get_scenario_data.py` script (in
the [`price_response`](https://github.com/switch-hawaii/price_response)
repository).


# Install a Proprietary Solver

We used the proprietary IBM CPLEX solver to solve the models used in the price response study. This is several orders of magnitude faster than
glpk or other open solvers (as of this writing). It has a free trial available, and is free long-term for
academics. More information on CPLEX can be found at the following links:

Professional:
- https://www.ibm.com/products/ilog-cplex-optimization-studio/pricing

Academic:
- https://developer.ibm.com/docloud/blog/2019/07/04/cplex-optimization-studio-for-students-and-academics/

You will need the unlimited-size version of this solver, which will require
either purchasing a license, using a time-limited trial version, or using an
academic-licensed version. The small-size free or community versions (1000
variables and constraints) will not be enough for this work.

The `options.txt` file in the [`price_response model
repository`](https://github.com/switch-hawaii/price_response) includes settings
for the CPLEX solver, including a special flag to tell Switch to retrieve dual
values after solving integer models. It may be possible to solve this model
using Gurobi, but you will need to implement similar functionality. (In some
cases, we have found that Switch/Pyomo can obtain dual values from the AMPL
version of GPLEX or Gurobi without any additional settings.)


# Solving the price response models
See the [`price response model
repository`](https://github.com/switch-hawaii/price_response) for instructions
to download the Switch input data used for the price response study and solve
the model.
