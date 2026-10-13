/**
 * Win32 system-level mouse control for DeepSeek Harness.
 *
 * Why this exists
 * ---------------
 * JavaScript `element.click()` produces `isTrusted === false`, which is the
 * first thing anti-automation scripts check. This plugin injects input at the
 * operating-system level via `SendInput`, so a real browser sees genuine
 * trusted events with real screen coordinates and a full hover -> mousedown ->
 * click sequence.
 *
 * Architecture
 * ------------
 * The tool logic lives in a bundled Python script (`python/winmouse.py`) that
 * uses only the standard library and `ctypes`. Node spawns it per call and
 * returns the parsed JSON. Python was chosen because ctypes exposes the Win32
 * input APIs directly; doing this in pure Node would require a native addon.
 *
 * Requires a visible, interactive desktop session: system-level input cannot
 * be delivered to a locked or headless session.
 *
 * @module dsh-plugin-mousectl
 */

import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const SCRIPT = join(HERE, '..', 'python', 'winmouse.py');

/** Cordis plugin name. */
export const name = 'mousectl';

/** Services this plugin needs; `subprocess` is used only to resolve Python. */
export const inject = ['tools'];

/**
 * Run the bundled Python engine and parse its JSON result.
 *
 * The engine always prints a single JSON object on stdout, so failures are
 * reported as data rather than as a non-zero exit we would have to guess at.
 *
 * @param args - CLI arguments for winmouse.py.
 * @param timeoutMs - hard timeout; system input can block on a wedged desktop.
 * @returns the parsed engine result.
 */
function runEngine(args, timeoutMs = 60_000) {
  return new Promise((resolve, reject) => {
    if (!existsSync(SCRIPT)) {
      reject(new Error(`mousectl engine missing at ${SCRIPT}`));
      return;
    }
    const child = spawn(PYTHON, [SCRIPT, ...args], {
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    let out = '';
    let err = '';
    const timer = setTimeout(() => {
      child.kill();
      reject(new Error(`mousectl engine timed out after ${timeoutMs}ms`));
    }, timeoutMs);

    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk) => { out += chunk; });
    child.stderr.on('data', (chunk) => { err += chunk; });

    child.on('error', (error) => {
      clearTimeout(timer);
      reject(new Error(`could not start mousectl engine: ${error.message}`));
    });

    child.on('close', (code) => {
      clearTimeout(timer);
      const text = out.trim();
      const start = text.indexOf('{');
      if (start < 0) {
        // No JSON: surface whatever the engine explained on either stream so
        // the model sees the real reason instead of a bare exit code.
        const detail = err.trim() || text || '(no output)';
        reject(new Error(`mousectl engine failed (exit ${code}): ${detail}`));
        return;
      }
      try {
        resolve(JSON.parse(text.slice(start)));
      } catch (error) {
        reject(new Error(`mousectl engine returned invalid JSON: ${error.message}`));
      }
    });
  });
}

/**
 * Locate a usable Python interpreter.
 *
 * Preference order: an explicit override, DSH's bundled runtime (always
 * present in a desktop install), then whatever is on PATH.
 */
function resolvePython() {
  if (process.env.DSH_MOUSECTL_PYTHON) return process.env.DSH_MOUSECTL_PYTHON;
  const candidates = [
    join(process.env.USERPROFILE ?? '', '.dsh', 'dsh-runtimes', 'dsh-primary-runtime',
      'dependencies', 'python', 'python.exe'),
    join(process.env.LOCALAPPDATA ?? '', 'Programs', 'Python', 'Python312', 'python.exe'),
    'python.exe',
    'python3',
  ];
  for (const candidate of candidates) {
    if (candidate.startsWith(join(process.env.USERPROFILE ?? '', '.dsh')) && !existsSync(candidate)) {
      continue;
    }
    return candidate;
  }
  return 'python.exe';
}

const PYTHON = resolvePython();

/** Shared parameter fragment for the failsafe toggle. */
const FAILSAFE_PARAM = {
  type: 'boolean',
  description:
    'Abort if the cursor is slammed into a screen corner. Defaults to true; '
    + 'pass false only for unattended runs where nothing else drives the mouse.',
};

/** Shared parameter fragment for the humanized movement duration. */
const DURATION_PARAM = {
  type: 'number',
  description:
    'Seconds spent travelling to the target. Larger is slower and more human. '
    + 'Defaults to 0.25 for clicks, 0.3 for moves.',
};

/**
 * Register the mouse-control tools on `ctx.tools`.
 *
 * @param ctx - registrant context carrying the tool registry.
 */
export function apply(ctx) {
  ctx.tools.register({
    name: 'mouse_click',
    description:
      'Click the mouse using real system-level input (Win32 SendInput), not synthetic DOM events. '
      + 'Targets a screen position or a window-relative point, moves along a curved humanized path first '
      + '(producing genuine hover and mousedown ordering), and reports where it clicked. '
      + 'Requires a visible interactive desktop session. The physical cursor is moved.',
    parameters: {
      x: { type: 'number', description: 'Absolute screen X. Required unless window is given.' },
      y: { type: 'number', description: 'Absolute screen Y. Required unless window is given.' },
      window: {
        type: 'string',
        description:
          'Window title substring. When given without x/y, clicks a point inside that window, '
          + 'resolved from its live screen rectangle. Invisible characters in Chromium titles are handled.',
      },
      offset_x: {
        type: 'number',
        description: 'Fraction across the window (0..1) when using window. Defaults to a random 0.3..0.7.',
      },
      offset_y: {
        type: 'number',
        description: 'Fraction down the window (0..1) when using window. Defaults to a random 0.3..0.7.',
      },
      button: {
        type: 'string',
        enum: ['left', 'right', 'middle'],
        description: 'Mouse button. Defaults to left.',
      },
      clicks: { type: 'integer', description: 'Number of clicks, e.g. 2 for a double click. Defaults to 1.' },
      duration: DURATION_PARAM,
      no_failsafe: FAILSAFE_PARAM,
      dry_run: { type: 'boolean', description: 'Report the resolved target without moving the mouse.' },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        // Output schemas are compiled with `allowRequired: false`, so no
        // `required` keyword is permitted here at all.
        properties: {
          ok: { type: 'boolean' },
          clicked: { type: 'boolean' },
          x: { type: 'number' },
          y: { type: 'number' },
          button: { type: 'string' },
          clicks: { type: 'integer' },
          target: { type: 'string' },
        },
      },
      render: (_args, value) => [{
        type: 'text',
        text: value.clicked
          ? `Clicked ${value.button} x${value.clicks} at screen (${value.x}, ${value.y}).`
          : `Dry run: would click ${value.button} x${value.clicks} at screen (${value.x}, ${value.y}).`,
      }],
    },
    isConcurrencySafe: () => false,
    timeoutMs: 60_000,
    async execute(args) {
      const argv = ['click'];
      let resolved;

      if (typeof args.window === 'string' && args.window.length > 0) {
        resolved = await runEngine(['wrect', '--window', args.window, '--json']);
        if (resolved.ok !== true) {
          throw new Error(
            `no visible window matching ${JSON.stringify(args.window)}. `
            + 'Use mouse_windows to list candidates.',
          );
        }
      }

      if (typeof args.x === 'number' && typeof args.y === 'number') {
        argv.push('--x', String(Math.round(args.x)), '--y', String(Math.round(args.y)));
      } else if (resolved) {
        const fx = args.offset_x ?? (0.3 + Math.random() * 0.4);
        const fy = args.offset_y ?? (0.3 + Math.random() * 0.4);
        // Windows may report minimized windows at -32000; refuse rather than
        // clicking off-screen and silently hitting nothing.
        if (resolved.left <= -30000 || resolved.top <= -30000) {
          throw new Error(
            `window ${JSON.stringify(args.window)} appears minimized (rect at `
            + `${resolved.left},${resolved.top}); restore it before clicking.`,
          );
        }
        const cx = Math.round(resolved.left + resolved.width * fx);
        const cy = Math.round(resolved.top + resolved.height * fy);
        argv.push('--x', String(cx), '--y', String(cy));
      } else {
        throw new Error('mouse_click needs either x and y, or a window title.');
      }

      argv.push('--button', args.button ?? 'left');
      argv.push('--clicks', String(args.clicks ?? 1));
      if (typeof args.duration === 'number') argv.push('--duration', String(args.duration));
      if (args.no_failsafe === true) argv.push('--no-failsafe');
      if (args.dry_run === true) argv.push('--dry-run');
      // Always request JSON: the engine prints human-readable text otherwise,
      // and runEngine parses stdout as JSON.
      argv.push('--json');

      const result = await runEngine(argv);
      if (result.ok !== true) throw new Error(result.error ?? 'mousectl click failed');
      return {
        ok: true,
        clicked: args.dry_run !== true,
        x: result.x,
        y: result.y,
        button: args.button ?? 'left',
        clicks: args.clicks ?? 1,
        target: typeof args.window === 'string' ? args.window : 'absolute',
      };
    },
    presentCall: (args) => ({
      card: 'generic',
      title: args.window ? `Click in ${args.window}` : `Click (${args.x}, ${args.y})`,
      kind: 'other',
      rawInput: args,
    }),
  });

  ctx.tools.register({
    name: 'mouse_move',
    description:
      'Move the mouse cursor to an absolute screen position with real system-level input, '
      + 'optionally along a curved humanized path. Requires a visible interactive desktop session.',
    parameters: {
      x: { type: 'number', required: true, description: 'Absolute screen X.' },
      y: { type: 'number', required: true, description: 'Absolute screen Y.' },
      duration: DURATION_PARAM,
      no_failsafe: FAILSAFE_PARAM,
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          ok: { type: 'boolean' },
          x: { type: 'number' },
          y: { type: 'number' },
          previous_x: { type: 'number' },
          previous_y: { type: 'number' },
        },
      },
      render: (_args, value) => [{
        type: 'text',
        text: `Moved cursor to (${value.x}, ${value.y}).`,
      }],
    },
    isConcurrencySafe: () => false,
    timeoutMs: 30_000,
    async execute(args) {
      const before = await runEngine(['pos', '--json']);
      const argv = ['move', '--x', String(Math.round(args.x)), '--y', String(Math.round(args.y))];
      if (typeof args.duration === 'number') argv.push('--duration', String(args.duration));
      if (args.no_failsafe === true) argv.push('--no-failsafe');
      argv.push('--json');
      const result = await runEngine(argv);
      if (result.ok !== true) throw new Error(result.error ?? 'mousectl move failed');
      return {
        ok: true,
        x: result.x,
        y: result.y,
        previous_x: before?.x,
        previous_y: before?.y,
      };
    },
  });

  ctx.tools.register({
    name: 'mouse_windows',
    description:
      'List visible top-level windows, or report one window\'s exact screen rectangle. '
      + 'Use this to find the browser window and compute where to click. '
      + 'Title matching ignores invisible characters such as the zero-width space in Chromium titles.',
    parameters: {
      filter: { type: 'string', description: 'Only windows whose title contains this text.' },
      rect: {
        type: 'string',
        description: 'Return the screen rectangle of the first window matching this title instead of a list.',
      },
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          ok: { type: 'boolean' },
          windows: {
            type: 'array',
            items: {
              type: 'object',
              additionalProperties: false,
              properties: {
                title: { type: 'string' },
                hwnd: { type: 'integer' },
                left: { type: 'number' }, top: { type: 'number' },
                width: { type: 'number' }, height: { type: 'number' },
                minimized: { type: 'boolean' },
              },
            },
          },
          screen_width: { type: 'number' },
          screen_height: { type: 'number' },
        },
      },
      render: (_args, value) => {
        if (value.windows.length === 0) {
          return [{ type: 'text', text: 'No matching visible windows.' }];
        }
        const lines = value.windows.map((w) => (
          `  ${w.title}\n    rect=(${w.left},${w.top}) size=${w.width}x${w.height}`
          + `${w.minimized ? '  [MINIMIZED]' : ''}`
        ));
        return [{
          type: 'text',
          text: `Visible windows (screen ${value.screen_width}x${value.screen_height}):\n${lines.join('\n')}`,
        }];
      },
    },
    timeoutMs: 30_000,
    async execute(args) {
      const [rect, list] = await Promise.all([
        runEngine(['screen', '--json']),
        runEngine(['windows', '--json', ...(args.filter ? ['--filter', args.filter] : [])]),
      ]);
      if (list.ok !== true) throw new Error(list.error ?? 'mousectl windows failed');

      let windows = list.windows ?? [];
      // Attach each window's rectangle so one call answers "where do I click".
      windows = await Promise.all(windows.map(async (w) => {
        try {
          const r = await runEngine(['wrect', '--window', w.title, '--json']);
          const minimized = r.ok === true && (r.left <= -30000 || r.top <= -30000);
          return {
            title: w.title, hwnd: w.hwnd,
            left: r.left, top: r.top, width: r.width, height: r.height,
            minimized,
          };
        } catch {
          return { title: w.title, hwnd: w.hwnd, left: 0, top: 0, width: 0, height: 0, minimized: false };
        }
      }));

      if (args.rect) {
        const want = args.rect.toLowerCase();
        windows = windows.filter((w) => w.title.toLowerCase().includes(want));
        if (windows.length === 0) {
          throw new Error(`no visible window matching ${JSON.stringify(args.rect)}`);
        }
        windows = [windows[0]];
      }

      return {
        ok: true,
        windows,
        screen_width: rect?.width ?? 0,
        screen_height: rect?.height ?? 0,
      };
    },
  });

  ctx.tools.register({
    name: 'mouse_scroll',
    description:
      'Scroll the mouse wheel using real system-level input, optionally after moving to a position first. '
      + 'Positive scrolls up, negative scrolls down. One notch is a typical wheel click.',
    parameters: {
      amount: { type: 'integer', required: true, description: 'Notches to scroll; positive is up.' },
      x: { type: 'number', description: 'Screen X to move to before scrolling.' },
      y: { type: 'number', description: 'Screen Y to move to before scrolling.' },
      no_failsafe: FAILSAFE_PARAM,
    },
    output: {
      schema: {
        type: 'object',
        additionalProperties: false,
        properties: {
          ok: { type: 'boolean' },
          amount: { type: 'integer' },
        },
      },
      render: (_args, value) => [{
        type: 'text',
        text: `Scrolled ${value.amount} notch(es) ${value.amount >= 0 ? 'up' : 'down'}.`,
      }],
    },
    isConcurrencySafe: () => false,
    timeoutMs: 30_000,
    async execute(args) {
      const argv = ['scroll', '--amount', String(Math.round(args.amount))];
      if (typeof args.x === 'number' && typeof args.y === 'number') {
        argv.push('--x', String(Math.round(args.x)), '--y', String(Math.round(args.y)));
      }
      if (args.no_failsafe === true) argv.push('--no-failsafe');
      argv.push('--json');
      const result = await runEngine(argv);
      if (result.ok !== true) throw new Error(result.error ?? 'mousectl scroll failed');
      return { ok: true, amount: Math.round(args.amount) };
    },
  });
}
