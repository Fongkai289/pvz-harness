"""Import your own 800x600 menu screenshots and crop your shovel template; no game controls."""
import argparse
from pathlib import Path
import shutil
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import cv2
from server.geometry import SHOVEL_W, SHOVEL_H, SHOVEL_XS


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screens',type=Path,help='Folder containing your main_menu.png, continue_game.jpg, game_paused.jpg, game_over.jpg')
    parser.add_argument('--shovel-frame',type=Path,help='800x600 gameplay frame with shovel resting in its box')
    parser.add_argument('--shovel-x',type=int,choices=SHOVEL_XS)
    args=parser.parse_args()
    if not args.screens and not args.shovel_frame:parser.error('supply --screens and/or --shovel-frame')
    def read(path):
        value=cv2.imread(str(path))
        if value is None or value.shape!=(600,800,3):parser.error(f'{path}: expected 800x600 BGR game client image')
        return value
    if args.screens:
        names=('main_menu.png','continue_game.jpg','game_paused.jpg','game_over.jpg')
        for name in names:read(args.screens/name)
        out=ROOT/'docs/screens';out.mkdir(parents=True,exist_ok=True)
        for name in names:
            source=(args.screens/name).resolve();dest=(out/name).resolve()
            if source!=dest:shutil.copyfile(source,dest)
    if args.shovel_frame:
        if args.shovel_x is None:parser.error('--shovel-frame requires --shovel-x')
        image=read(args.shovel_frame)
        out=ROOT/'knowledge/shovel_box.png'
        out.parent.mkdir(parents=True,exist_ok=True)
        if not cv2.imwrite(str(out),image[:SHOVEL_H,args.shovel_x:args.shovel_x+SHOVEL_W]):raise OSError('Could not write shovel crop')
    print('Local assets prepared; these files are ignored by Git. See docs/assets.md for card templates.')


if __name__=='__main__':main()
