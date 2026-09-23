#!/usr/bin/env python3
import os
import re
import sys
import shutil
import asyncio
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import async_playwright

# ==========================================================
# Paths
# ==========================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ENV_PATH = os.path.join(SCRIPT_DIR, '.env')
FALLBACK_TXT_PATH = os.path.join(SCRIPT_DIR, 'inferhub_prices.txt')

# ==========================================================
# ANSI colors
# ==========================================================
RESET = '\033[0m'
ANSI = {
    'black': '\033[30m',
    'red': '\033[31m',
    'green': '\033[32m',
    'yellow': '\033[33m',
    'blue': '\033[34m',
    'magenta': '\033[35m',
    'cyan': '\033[36m',
    'white': '\033[37m',

    'bright_black': '\033[90m',
    'bright_red': '\033[91m',
    'bright_green': '\033[92m',
    'bright_yellow': '\033[93m',
    'bright_blue': '\033[94m',
    'bright_magenta': '\033[95m',
    'bright_cyan': '\033[96m',
    'bright_white': '\033[97m',

    'dark_gray': '\033[90m',
    'gray': '\033[38;5;245m',
    'grey': '\033[38;5;245m',

    'orange': '\033[38;5;208m',
    'purple': '\033[38;5;135m',
    'pink': '\033[38;5;213m',
    'teal': '\033[38;5;37m',
    'gold': '\033[38;5;220m',
}


def enable_ansi():
    """Enable ANSI color support on Windows consoles."""
    if os.name != 'nt':
        return

    try:
        import ctypes
        h = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetConsoleMode(h, ctypes.byref(mode)):
            ctypes.windll.kernel32.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        os.system('')


# ==========================================================
# Env discovery / parsing
# ==========================================================
def find_env_path(cli_path: Optional[str] = None) -> Optional[str]:
    """
    Find .env in this priority:
      1. CLI argument
      2. .env next to prices.py
      3. ./.env
      4. ~/.omp/.env
      5. ~/.omp/agent/.env
    """
    candidates = []

    if cli_path:
        candidates.append(cli_path)

    candidates.extend([
        DEFAULT_ENV_PATH,
        os.path.abspath('.env'),
        os.path.expanduser('~/.omp/.env'),
        os.path.expanduser('~/.omp/agent/.env'),
    ])

    seen = set()
    for candidate in candidates:
        if not candidate:
            continue

        candidate = os.path.expanduser(candidate)
        if candidate in seen:
            continue

        seen.add(candidate)
        if os.path.exists(candidate):
            return candidate

    return None


def load_env_config(filepath: Optional[str]) -> Tuple[Optional[List[Dict[str, Any]]], Dict[str, str]]:
    """
    Parses the .env file.

    Model line format:
      MODEL_ID | COMPANY | POWER(0-100) | CONTEXT | MAXTOKENS

    Color section:
      [colors]
      company = colorname

    Returns:
      (ordered model list or None, colors dict)
    """
    if not filepath or not os.path.exists(filepath):
        return None, {}

    models = []
    colors = {}
    section = 'models'

    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            s = line.strip()

            if not s or s.startswith('#'):
                continue

            if s.lower() in ('[colors]', 'colors:'):
                section = 'colors'
                continue

            if section == 'colors':
                if '=' in s:
                    company, _, color = s.partition('=')
                    colors[company.strip().lower()] = color.strip().lower()
                continue

            parts = [p.strip() for p in s.split('|')]
            if not parts or not parts[0]:
                continue

            model = parts[0]
            raw_fields = parts[1:]

            company = ''
            numeric_fields = []

            # If the second field is not numeric, treat it as company.
            # This also tolerates lines where POWER was accidentally omitted.
            if raw_fields and not re.fullmatch(r'[\d,]+', raw_fields[0].strip()):
                company = raw_fields[0].strip().lower()
                numeric_fields = raw_fields[1:]
            else:
                numeric_fields = raw_fields

            nums = []
            for item in numeric_fields:
                cleaned = item.strip().replace(',', '')
                if re.fullmatch(r'\d+', cleaned):
                    nums.append(int(cleaned))

            power = None
            context_window = None
            max_tokens = None

            # Preferred new format:
            #   POWER | CONTEXT | MAXTOKENS
            #
            # Also tolerate:
            #   CONTEXT | MAXTOKENS
            #
            # And old format:
            #   POWER only
            if len(nums) >= 3:
                if nums[0] <= 100:
                    power = nums[0]
                    context_window = nums[1]
                    max_tokens = nums[2]
                else:
                    context_window = nums[0]
                    max_tokens = nums[1]

            elif len(nums) == 2:
                # If it looks like POWER + CONTEXT, accept that.
                # Otherwise assume CONTEXT + MAXTOKENS.
                if nums[0] <= 100 and nums[1] > 100:
                    power = nums[0]
                    context_window = nums[1]
                else:
                    context_window = nums[0]
                    max_tokens = nums[1]

            elif len(nums) == 1:
                if nums[0] <= 100:
                    power = nums[0]
                else:
                    context_window = nums[0]

            models.append({
                'modelId': model,
                'company': company,
                'power': power,
                'contextWindow': context_window,
                'maxTokens': max_tokens,
            })

    return (models if models else None), colors


# ==========================================================
# Price helpers
# ==========================================================
def parse_price(value: Optional[str]) -> Optional[float]:
    """Parse '$1.23', '1,234.56', etc. into float."""
    if value is None:
        return None

    if value in ('N/A', 'Not Found', ''):
        return None

    match = re.search(r'[\d,.]+', str(value))
    if not match:
        return None

    try:
        return float(match.group(0).replace(',', ''))
    except ValueError:
        return None


# ==========================================================
# Browser scraping
# ==========================================================
JS_EXTRACT = r'''() => {
    const results = [];

    document.querySelectorAll('table').forEach(table => {
        Array.from(table.querySelectorAll('tr')).slice(1).forEach(row => {
            const cols = row.querySelectorAll('td');
            if (cols.length >= 3) {
                let modelId = "";

                const modelEl = cols[0].querySelector('code') || cols[0].querySelector('.font-mono');
                if (modelEl) {
                    const clone = modelEl.cloneNode(true);
                    clone.querySelectorAll('span').forEach(s => s.remove());
                    modelId = clone.innerText.split('\n')[0].trim();
                } else {
                    modelId = cols[0].innerText.split('\n')[0].trim();
                }

                const extractLowest = (cell) => {
                    const priceRows = Array.from(cell.querySelectorAll('div'))
                        .filter(div => div.querySelector('.min-w-14'));

                    const entries = [];

                    priceRows.forEach(rowEl => {
                        const text = rowEl.innerText;
                        const pm = text.match(/\$[\d,.]+/);
                        const am = text.match(/(\d+)\s*avail/i);

                        if (pm) {
                            entries.push({
                                priceStr: pm[0],
                                priceVal: parseFloat(pm[0].replace(/[$,]/g, '')),
                                avail: am ? parseInt(am[1], 10) : 0
                            });
                        }
                    });

                    if (entries.length === 0) {
                        return { price: "N/A", avail: "0" };
                    }

                    const minVal = Math.min(...entries.map(e => e.priceVal));
                    const totalAvail = entries
                        .filter(e => e.priceVal === minVal)
                        .reduce((s, e) => s + e.avail, 0);

                    const minStr = entries.find(e => e.priceVal === minVal).priceStr;

                    return { price: minStr, avail: String(totalAvail) };
                };

                const input = extractLowest(cols[1]);
                const output = extractLowest(cols[2]);

                results.push({
                    modelId: modelId,
                    inputPrice: input.price,
                    inputAvail: input.avail,
                    outputPrice: output.price,
                    outputAvail: output.avail
                });
            }
        });
    });

    return results;
}'''


async def scrape_inferhub_raw() -> List[Dict[str, str]]:
    """Scrape InferHub and return raw pricing rows."""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        await page.goto('https://inferhub.dev/pricing')
        await page.wait_for_selector('table')
        await page.wait_for_timeout(3000)

        data = await page.evaluate(JS_EXTRACT)
        await browser.close()

        return data or []


# ==========================================================
# Data shaping for OMP
# ==========================================================
def build_omp_models(scraped: List[Dict[str, Any]], env_models: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """
    Build full OMP model entries from scraped prices + .env metadata.

    Output item format:
      {
        'id': ...,
        'name': ...,
        'contextWindow': ...,
        'maxTokens': ...,
        'cost': {
          'input': ...,
          'output': ...,
          'cacheRead': ...,
          'cacheWrite': ...
        }
      }
    """
    lookup = {m.get('modelId'): m for m in scraped if m.get('modelId')}

    if env_models:
        sources = env_models
    else:
        sources = [
            {
                'modelId': m.get('modelId'),
                'contextWindow': None,
                'maxTokens': None,
            }
            for m in scraped
            if m.get('modelId')
        ]

    result = []

    for entry in sources:
        model_id = entry.get('modelId')
        if not model_id:
            continue

        base = lookup.get(model_id, {})

        input_price = parse_price(base.get('inputPrice'))
        output_price = parse_price(base.get('outputPrice'))

        if input_price is None:
            input_price = 0.0

        if output_price is None:
            output_price = 0.0

        result.append({
            'id': model_id,
            'name': model_id,
            'contextWindow': int(entry.get('contextWindow') or 0),
            'maxTokens': int(entry.get('maxTokens') or 0),
            'cost': {
                'input': input_price,
                'output': output_price,
                'cacheRead': input_price,
                'cacheWrite': input_price,
            }
        })

    return result


async def get_inferhub_models(env_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Public function used by omp.py.

    Returns full OMP-ready model dictionaries.
    """
    env_file = find_env_path(env_path)
    env_models, _ = load_env_config(env_file) if env_file else (None, {})

    scraped = await scrape_inferhub_raw()
    return build_omp_models(scraped, env_models)


# ==========================================================
# Display table support
# ==========================================================
def merge_display_data(scraped: List[Dict[str, Any]], env_models: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Merge scraped data with .env metadata for table display."""
    lookup = {m.get('modelId'): m for m in scraped if m.get('modelId')}

    if env_models:
        sources = env_models
    else:
        sources = [
            {
                'modelId': m.get('modelId'),
                'company': '',
                'power': None,
                'contextWindow': None,
                'maxTokens': None,
            }
            for m in scraped
            if m.get('modelId')
        ]

    rows = []

    for entry in sources:
        model_id = entry.get('modelId')
        if not model_id:
            continue

        base = lookup.get(model_id, {})

        rows.append({
            'modelId': model_id,
            'company': entry.get('company', ''),
            'power': entry.get('power'),
            'contextWindow': entry.get('contextWindow'),
            'maxTokens': entry.get('maxTokens'),

            'inputPrice': base.get('inputPrice', 'N/A'),
            'inputAvail': base.get('inputAvail', 'N/A'),

            'outputPrice': base.get('outputPrice', 'N/A'),
            'outputAvail': base.get('outputAvail', 'N/A'),
        })

    return rows


def sort_display_data(rows: List[Dict[str, Any]], using_env: bool) -> List[Dict[str, Any]]:
    """Sort rows for display."""
    if using_env:
        # Highest power first. Stable for equal power.
        rows.sort(key=lambda m: -(m.get('power') if m.get('power') is not None else -1))
    else:
        def price_key(row):
            value = parse_price(row.get('outputPrice'))
            return float('inf') if value is None else value

        rows.sort(key=price_key)

    return rows


def normalize_data(data: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize and align numeric display values."""
    pairs = [('inputPrice', 'inputAvail'), ('outputPrice', 'outputAvail')]
    max_decimals = 0

    for m in data:
        for key, _ in pairs:
            val = m.get(key)
            if val in (None, 'N/A'):
                continue

            n = re.search(r'[\d,]+(?:\.\d+)?', str(val))
            if n:
                n_clean = n.group(0).replace(',', '')
                if '.' in n_clean:
                    max_decimals = max(max_decimals, len(n_clean.split('.')[1]))

    parsed = []

    for m in data:
        row = {'power': m.get('power')}

        for pkey, akey in pairs:
            pv = m.get(pkey)
            av = m.get(akey)

            pm = re.search(r'[\d,.]+', str(pv)) if pv not in (None, 'N/A') else None
            am = re.search(r'\d+', str(av)) if av not in (None, 'N/A') else None

            row[pkey] = float(pm.group(0).replace(',', '')) if pm else None
            row[akey] = int(am.group(0)) if am else None

        parsed.append(row)

    max_price_width = 0
    max_prov_width = 0
    max_total_width = 0
    max_power_width = 0

    for row in parsed:
        for pkey, akey in pairs:
            if row[pkey] is not None:
                max_price_width = max(max_price_width, len(f"{row[pkey]:.{max_decimals}f}"))

            if row[akey] is not None:
                max_prov_width = max(max_prov_width, len(str(row[akey])))

        if row['inputPrice'] is not None and row['outputPrice'] is not None:
            max_total_width = max(
                max_total_width,
                len(f"{row['inputPrice'] + row['outputPrice']:.{max_decimals}f}")
            )

        if row['power'] is not None:
            max_power_width = max(max_power_width, len(str(row['power'])))

    for i, row in enumerate(parsed):
        m = data[i]

        for pkey, akey in pairs:
            if row[pkey] is not None:
                m[pkey] = '$' + f"{row[pkey]:.{max_decimals}f}".rjust(max_price_width)
            else:
                m[pkey] = 'N/A'

            if row[akey] is not None:
                m[akey] = str(row[akey]).rjust(max_prov_width)
            else:
                m[akey] = 'N/A'

        if row['inputPrice'] is not None and row['outputPrice'] is not None:
            total = row['inputPrice'] + row['outputPrice']
            m['totalPrice'] = '$' + f"{total:.{max_decimals}f}".rjust(max_total_width)
        else:
            m['totalPrice'] = 'N/A'

        m['power'] = str(row['power']).rjust(max_power_width) if row['power'] is not None else 'N/A'

    return data


def fit(s: str, w: int) -> str:
    """Fit string into width, truncating with '..' if needed."""
    if len(s) <= w:
        return s

    if w <= 2:
        return s[:w]

    return s[:w - 2] + '..'


def pick_header(candidates: List[str], width: int) -> str:
    """Pick the longest header that fits."""
    for c in candidates:
        if len(c) <= width:
            return c

    return fit(candidates[-1], width)


# Column specs: (data key, header candidates longest->shortest)
COLUMN_SPECS = [
    ('modelId',     ["Model"]),
    ('power',       ["Int"]),
    ('inputPrice',  ["Input"]),
    ('inputAvail',  ["Hosts"]),
    ('outputPrice', ["Output"]),
    ('outputAvail', ["Hosts"]),
    ('totalPrice',  ["Total"]),
]


def build_table(data: List[Dict[str, Any]], widths: List[int], colors: Optional[Dict[str, str]] = None) -> List[str]:
    """Build plain-text table lines."""
    header = ' | '.join(
        f"{pick_header(spec[1], width):<{width}}"
        for spec, width in zip(COLUMN_SPECS, widths)
    ) + ' |'

    lines = [header, '-' * len(header)]

    for m in data:
        code = ''

        if colors:
            company = str(m.get('company', '')).lower()
            code = ANSI.get(colors.get(company, ''), '')

        row_parts = []

        for spec, width in zip(COLUMN_SPECS, widths):
            key = spec[0]
            cell_text = f"{fit(str(m.get(key, '-')), width):<{width}}"

            if code:
                row_parts.append(f"{code}{cell_text}{RESET}")
            else:
                row_parts.append(cell_text)

        row = ' | '.join(row_parts) + ' |'
        lines.append(row)

    return lines


# ==========================================================
# CLI entrypoint
# ==========================================================
async def main() -> None:
    enable_ansi()

    env_arg = sys.argv[1] if len(sys.argv) > 1 else None
    env_path = find_env_path(env_arg)

    env_models, company_colors = load_env_config(env_path) if env_path else (None, {})

    print("Loading InferHub pricing page (waiting for JavaScript to render all tables)...")
    scraped = await scrape_inferhub_raw()

    data = merge_display_data(scraped, env_models)
    data = sort_display_data(data, bool(env_models))

    if env_models:
        print(f"Filtering by {len(env_models)} models from {env_path} (ranked by power, ties by .env order)...")
    else:
        print(f"No .env filter found - showing all models.")

    data = normalize_data(data)

    print(f"\nDisplaying {len(data)} models!\n")

    widths = [
        max((len(str(m.get(key, ''))) for m in data), default=len(candidates[0]))
        for key, candidates in COLUMN_SPECS
    ]

    # Auto-fit to terminal width. Shrinks Model ID column only.
    term_width = shutil.get_terminal_size().columns
    sep_width = 3 * (len(COLUMN_SPECS) - 1) + 2
    fixed_width = sum(widths[1:]) + sep_width

    console_widths = list(widths)

    if widths[0] + fixed_width > term_width:
        console_widths[0] = max(12, term_width - fixed_width)

    if term_width - fixed_width < 12:
        print(f"WARNING: terminal is only {term_width} columns wide; the data columns alone need {fixed_width}.")
        print("Tip: shrink your terminal font (Ctrl + -) or maximize the window.")
        print(f"Full untruncated table saved to {FALLBACK_TXT_PATH}\n")

        with open(FALLBACK_TXT_PATH, 'w', encoding='utf-8') as f:
            f.write('\n'.join(build_table(data, widths)))

    for line in build_table(data, console_widths, company_colors if env_models else None):
        print(line)


if __name__ == "__main__":
    asyncio.run(main())
