#!/usr/bin/env python3
import os
import sys
import asyncio
import subprocess
from typing import Any, Dict, List

try:
    from ruamel.yaml import YAML
    yaml_lib = 'ruamel'
except ImportError:
    import yaml
    yaml_lib = 'pyyaml'

import prices


# ==========================================================
# YAML helpers
# ==========================================================
def find_models_yml() -> str:
    """
    Find OMP models.yml.

    If none exists, default to:
      ~/.omp/agent/models.yml
    """
    paths_to_check = [
        os.path.expanduser('~/.omp/agent/models.yml'),
        os.path.expanduser('~/.omp/agent/models.yaml'),
        'models.yml',
        'models.yaml',
    ]

    for p in paths_to_check:
        if os.path.exists(p):
            return p

    return os.path.expanduser('~/.omp/agent/models.yml')


def ensure_inferhub_models(config: Any) -> Dict[str, Any]:
    """
    Ensure this structure exists:

    providers:
      inferhub:
        models: []
    """
    if config is None:
        config = {}

    if not hasattr(config, 'get'):
        config = {}

    providers = config.get('providers')
    if providers is None or not hasattr(providers, 'get'):
        config['providers'] = {}
        providers = config['providers']

    inferhub = providers.get('inferhub')
    if inferhub is None or not hasattr(inferhub, 'get'):
        providers['inferhub'] = {}
        inferhub = providers['inferhub']

    models = inferhub.get('models')
    if models is None or not hasattr(models, 'append'):
        inferhub['models'] = []

    return config


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def apply_models(existing_models: List[Any], incoming_models: List[Dict[str, Any]]) -> tuple[int, int]:
    """
    Merge incoming models into existing YAML models.

    - Updates models with matching id.
    - Appends models that do not exist yet.
    - Does not delete unrelated existing models.
    """
    index = {}

    for i, model in enumerate(existing_models):
        if not hasattr(model, 'get'):
            continue

        mid = model.get('id')
        if mid:
            index[mid] = i

    updated = 0
    added = 0

    for incoming in incoming_models:
        mid = incoming.get('id')
        if not mid:
            continue

        context_window = incoming.get('contextWindow')
        max_tokens = incoming.get('maxTokens')
        cost = incoming.get('cost', {})

        if mid in index:
            model = existing_models[index[mid]]

            if not hasattr(model, 'get'):
                model = {}
                existing_models[index[mid]] = model

            model['id'] = mid
            model['name'] = mid

            # Only overwrite context/max if incoming has useful values.
            if context_window:
                model['contextWindow'] = int(context_window)
            elif 'contextWindow' not in model:
                model['contextWindow'] = 0

            if max_tokens:
                model['maxTokens'] = int(max_tokens)
            elif 'maxTokens' not in model:
                model['maxTokens'] = 0

            cost_obj = model.get('cost')
            if cost_obj is None or not hasattr(cost_obj, 'get'):
                model['cost'] = {}
                cost_obj = model['cost']

            input_price = _as_float(cost.get('input'), 0.0)
            output_price = _as_float(cost.get('output'), 0.0)

            cost_obj['input'] = input_price
            cost_obj['output'] = output_price
            cost_obj['cacheRead'] = _as_float(cost.get('cacheRead', input_price), input_price)
            cost_obj['cacheWrite'] = _as_float(cost.get('cacheWrite', input_price), input_price)

            updated += 1

        else:
            existing_models.append({
                'id': mid,
                'name': mid,
                'contextWindow': int(context_window or 0),
                'maxTokens': int(max_tokens or 0),
                'cost': {
                    'input': _as_float(cost.get('input'), 0.0),
                    'output': _as_float(cost.get('output'), 0.0),
                    'cacheRead': _as_float(cost.get('cacheRead', cost.get('input')), 0.0),
                    'cacheWrite': _as_float(cost.get('cacheWrite', cost.get('input')), 0.0),
                }
            })

            added += 1

    return updated, added


def update_models_yml(omp_models: List[Dict[str, Any]]) -> bool:
    """
    Write OMP model data into models.yml.

    This performs a merge:
      - existing matching models are updated
      - missing models are appended
      - other existing models are preserved
    """
    yaml_path = find_models_yml()

    print(f"[*] Updating OMP models in {yaml_path}...")

    yaml_parser = None
    config = None

    if yaml_lib == 'ruamel':
        yaml_parser = YAML()
        yaml_parser.preserve_quotes = True
        yaml_parser.indent(mapping=2, sequence=4, offset=2)

        if os.path.exists(yaml_path):
            with open(yaml_path, 'r', encoding='utf-8') as f:
                config = yaml_parser.load(f)
        else:
            config = {}

    else:
        if os.path.exists(yaml_path):
            with open(yaml_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
        else:
            config = {}

    config = ensure_inferhub_models(config)

    models_list = config['providers']['inferhub']['models']
    updated, added = apply_models(models_list, omp_models)

    # Ensure destination directory exists if we are creating a new file.
    destination_dir = os.path.dirname(os.path.abspath(yaml_path))
    if destination_dir:
        os.makedirs(destination_dir, exist_ok=True)

    if yaml_lib == 'ruamel':
        with open(yaml_path, 'w', encoding='utf-8') as f:
            yaml_parser.dump(config, f)
    else:
        with open(yaml_path, 'w', encoding='utf-8') as f:
            yaml.safe_dump(
                config,
                f,
                sort_keys=False,
                default_flow_style=False,
                allow_unicode=True,
            )

    print(f"    -> Updated {updated} existing models.")
    print(f"    -> Added {added} new models.")
    print("[*] YAML update complete.")

    return True


# ==========================================================
# Main
# ==========================================================
async def main() -> None:
    env_arg = sys.argv[1] if len(sys.argv) > 1 else None

    print("[*] Fetching InferHub data through prices.py...")

    try:
        omp_models = await prices.get_inferhub_models(env_arg)
    except Exception as exc:
        print(f"[!] Failed to fetch InferHub data: {exc}")
        omp_models = []

    if omp_models:
        update_models_yml(omp_models)
    else:
        print("[!] No model data was fetched; skipping YAML update.")

    print("\n[*] Scraping finished. Launching OMP...\n")

    try:
        if os.name == 'nt':
            subprocess.call('omp', shell=True)
        else:
            subprocess.call(['omp'])
    except Exception as e:
        print(f"[!] Failed to launch OMP: {e}")


if __name__ == "__main__":
    asyncio.run(main())
