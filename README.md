# Cartographer

## Requirements
- python3

## User Installation 
```
pip install xtal-cartographer
cartographer-install -o site_packages --all
```

## Usage

```
cartographer -i PDB.mtz -o PDB.map -intensity FWT -phase PHWT
```


```
usage: cartographer [-h] [-m M] -i I -o O [-r [R]] [-intensity [INTENSITY]] [-phase [PHASE]]

options:
  -h, --help              show this help message and exit
  -m M, -model_path M     Path to model
  -i I, -input I          Input mtz path
  -o O, -output O         Output map path 
  -r [R], -resolution [R] Resolution cutoff to apply to mtz
  -intensity [INTENSITY]  Name of intensity column in MTZ
  -phase [PHASE]          Name of phase column in MTZ
```

```
usage: cartographer-install [-h] -m {phos,sugar,base} [-o {site_packages,ccp4}] [--all] [--reinstall]

Cartographer Install

options:
  -h, --help            show this help message and exit
  -m {phos,sugar,base}, --model {phos,sugar,base}
  -o {site_packages,ccp4}, --output {site_packages,ccp4}
  --all
  --reinstall
```

## Developer Installation 
Clone the project

```
git clone https://github.com/Dialpuri/Cartographer.git
```

Change directories into Cartographer

```
cd Cartographer
```

Create a Python virtual environment and entire the environment

```
python3 -m virtualenv pyenv
source pyenv/bin/activate
```
Install using pip

```
cd cartographer
pip install .
```

