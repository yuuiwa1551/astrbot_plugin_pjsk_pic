"""Score already-collected predictions locally; does not call a model."""
import argparse
import json
import sys
import types
from pathlib import Path

pkg = types.ModuleType('pjsk_eval_tools')
pkg.__path__ = [str(Path(__file__).resolve().parents[1] / 'core')]
sys.modules[pkg.__name__] = pkg
from pjsk_eval_tools.identity_evaluation import evaluate_identity

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--manifest', required=True, type=Path)
parser.add_argument('--predictions', required=True, type=Path, nargs='+')
parser.add_argument('--split', choices=['holdout', 'development'], default='holdout')
args = parser.parse_args()
manifest = json.loads(args.manifest.read_text(encoding='utf-8-sig'))
results = {}
for path in args.predictions:
    predictions = json.loads(path.read_text(encoding='utf-8-sig'))
    try:
        results[str(path)] = evaluate_identity(manifest, predictions, split=args.split)
    except ValueError as exc:
        parser.error(str(exc))
print(json.dumps(results, ensure_ascii=False, indent=2))
