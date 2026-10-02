import {execFileSync} from 'node:child_process';
import {existsSync} from 'node:fs';
import {git} from './plan.mjs';

const changed = new Set(git('diff', '--name-only', process.env.BASE_SHA, 'HEAD', '--', 'plugins').split('\n').map(path => path.split('/')[1]));
for (const dir of changed) {
  if (!dir) continue;
  const tests = `plugins/${dir}/tests/run-tests.sh`;
  if (existsSync(tests)) execFileSync('bash', [tests], {stdio: 'inherit'});
}
