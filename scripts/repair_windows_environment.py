"""Repair generated venv launchers after moving this workspace; never reinstall packages."""
from importlib.metadata import distributions
from pathlib import Path
import os
import sys
import venv


def main():
    root=Path(__file__).resolve().parents[1]
    environment=root/'.venv'
    if os.name != 'nt' or Path(sys.prefix).resolve() != environment:
        raise SystemExit('Run this with the moved app/.venv/Scripts/python.exe on Windows')
    from pip._vendor.distlib.scripts import ScriptMaker
    builder=venv.EnvBuilder(with_pip=False)
    context=builder.ensure_directories(str(environment))
    builder.create_configuration(context)
    builder.setup_scripts(context)
    maker=ScriptMaker(None,str(environment/'Scripts'))
    maker.executable=str(environment/'Scripts/python.exe')
    maker.clobber=True
    maker.variants={'', 'X', 'X.Y'}
    count=0
    for distribution in distributions():
        for entry in distribution.entry_points:
            if entry.group in ('console_scripts','gui_scripts'):
                maker.make(f'{entry.name} = {entry.value}', options={'gui':entry.group=='gui_scripts'})
                count+=1
    print(f'Repaired activation scripts and {count} installed entry points')


if __name__=='__main__':
    main()
