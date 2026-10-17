#!/usr/bin/env node
'use strict';
// npx zoorik assess coral: runs the zoorik package from PyPI in a private Python environment, so nothing
// is installed into the system Python. The environment lives in ~/.cache/zoorik and is updated each run.
// -I keeps the current folder and PYTHON* variables out of it.
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const WINDOWS = process.platform === 'win32';
const ENV = path.join(os.homedir(), '.cache', 'zoorik', 'python');
const PYTHON = path.join(ENV, WINDOWS ? 'Scripts' : 'bin', WINDOWS ? 'python.exe' : 'python');
const AT_LEAST_38 = 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)';

function fail(message) {
  process.stderr.write(`zoorik: ${message}\n`);
  process.exit(1);
}

function systemPython() {
  const candidates = WINDOWS ? [['py', '-3'], ['python']] : [['python3'], ['python']];
  return candidates.find(([cmd, ...args]) => spawnSync(cmd, [...args, '-c', AT_LEAST_38]).status === 0);
}

if (!fs.existsSync(PYTHON)) {
  const python = systemPython();
  if (!python) fail('needs Python 3.8 or later. Install it from python.org, or run this in Azure Cloud Shell or AWS CloudShell.');
  const [cmd, ...args] = python;
  if (spawnSync(cmd, [...args, '-m', 'venv', ENV], { stdio: 'inherit' }).status !== 0) {
    fail(`could not create a Python environment in ${ENV}.`);
  }
}
const install = spawnSync(PYTHON, ['-I', '-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '--upgrade', 'zoorik'],
  { stdio: 'inherit' });
if (install.status !== 0) fail('could not install the zoorik package from PyPI.');

const run = spawnSync(PYTHON, ['-I', '-m', 'zoorik', ...process.argv.slice(2)], { stdio: 'inherit' });
process.exit(run.status === null ? 1 : run.status);
