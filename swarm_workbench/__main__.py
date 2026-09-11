import argparse
import json
import sys
from pathlib import Path
from . import monitor as monitor_view
from . import webui as web_view
from .blueprints import rank, warm_start
from .sssf import export_blueprint


def main():
    parser=argparse.ArgumentParser(description='SSSF blueprint extraction and warm starts')
    sub=parser.add_subparsers(dest='command',required=True)
    extract=sub.add_parser('extract'); extract.add_argument('run_id')
    extract.add_argument('--annotations'); extract.add_argument('--db',default='adws/adw_data/sssf.db')
    search=sub.add_parser('search'); search.add_argument('goal'); search.add_argument('--concept',action='append',default=[])
    warm=sub.add_parser('warm'); warm.add_argument('blueprint'); warm.add_argument('goal'); warm.add_argument('--out')
    monitor_view.add_arguments(sub.add_parser('monitor',description=monitor_view.__doc__))
    web_view.add_arguments(sub.add_parser('webui',description=web_view.__doc__))
    args=parser.parse_args()
    if args.command=='monitor':
        return monitor_view.main(args)
    if args.command=='webui':
        return web_view.main(args)
    if args.command=='extract':
        annotation=json.loads(Path(args.annotations).read_text()) if args.annotations else None
        bp,path=export_blueprint(args.db,args.run_id,'blueprints',annotation)
        result={'path':str(path),'verified':bp['verified_success'],'semantic_status':bp['semantic_status']}
    elif args.command=='search':
        bps=[json.loads(p.read_text()) for p in Path('blueprints').glob('bp-*.json')]
        result=[{k:v for k,v in item.items() if k!='blueprint'} | {'id':item['blueprint']['id']}
                for item in rank(bps,args.goal,args.concept)]
    else:
        result=warm_start(json.loads(Path(args.blueprint).read_text()),args.goal,Path.cwd())
    text=json.dumps(result,indent=2)
    if getattr(args,'out',None):
        Path(args.out).write_text(text,encoding='utf-8')
    print(text)
    return 0


if __name__=='__main__':
    sys.exit(main() or 0)
