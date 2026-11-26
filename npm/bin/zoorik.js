#!/usr/bin/env node
'use strict';
// npx zoorik assess: runs the zoorik package from PyPI, at this launcher's own version, in a private Python
// environment in ~/.cache/zoorik, so nothing is installed into the system Python. The environment is made once, with
// py -3 on Windows and python3 elsewhere. -I keeps the current folder and PYTHON* variables out of Python.
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { version } = require('../package.json');

const WINDOWS = process.platform === 'win32';
const SYSTEM_PYTHON = WINDOWS ? ['py', '-3'] : ['python3'];
const ENV = path.join(os.homedir(), '.cache', 'zoorik', 'python');
const PYTHON = path.join(ENV, WINDOWS ? 'Scripts' : 'bin', WINDOWS ? 'python.exe' : 'python');
const AT_LEAST_38 = 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)';

function fail(message) {
  process.stderr.write(`zoorik: ${message}\n`);
  process.exit(1);
}

if (!fs.existsSync(PYTHON)) {
  const [command, ...args] = SYSTEM_PYTHON;
  if (spawnSync(command, [...args, '-I', '-c', AT_LEAST_38]).status !== 0) {
    fail(`needs Python 3.8 or later as ${SYSTEM_PYTHON.join(' ')}. Install it from python.org, or run this in Azure Cloud Shell or AWS CloudShell.`);
  }
  if (spawnSync(command, [...args, '-I', '-m', 'venv', ENV], { stdio: 'inherit' }).status !== 0) {
    // A failed venv can leave its python behind, which would skip this step on every later run.
    fs.rmSync(ENV, { recursive: true, force: true });
    fail(`could not create a Python environment in ${ENV}.${WINDOWS ? '' : ' On Debian or Ubuntu, install python3-venv first.'}`);
  }
}
// The exact version, so this launcher runs the package released with it; once it is there, pip leaves it as it is.
const install = spawnSync(PYTHON, ['-I', '-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', `zoorik==${version}`],
  { stdio: 'inherit' });
if (install.status !== 0) fail(`could not install zoorik ${version} from PyPI. If this keeps failing, delete ${ENV} and run it again.`);

// Ctrl-C reaches Python too, which stops cleanly and says what it changed; Node waits for it instead of exiting first.
process.on('SIGINT', () => {});
const run = spawnSync(PYTHON, ['-I', '-m', 'zoorik.cli', ...process.argv.slice(2)], { stdio: 'inherit' });
process.exit(run.status === null ? 1 : run.status);
