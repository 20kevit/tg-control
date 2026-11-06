const assert = require('node:assert/strict');
const { chromium } = require('playwright-core');
const path = require('node:path');
// The bundled AWS-compatible libraries supply NSS in minimal Linux CI images.
process.env.AWS_EXECUTION_ENV = 'AWS_Lambda_nodejs22.x';
const chromiumPackage = require('@sparticuz/chromium');
const executable = chromiumPackage.default;

(async () => {
  await chromiumPackage.inflate(path.join(path.dirname(require.resolve('@sparticuz/chromium')), '../bin/al2023.tar.br'));
  const args = executable.args.filter(arg => !['--disable-web-security', '--allow-running-insecure-content'].includes(arg));
  const browser = await chromium.launch({ executablePath: await executable.executablePath(), args, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(process.env.TG_TEST_URL + '/admin/', { waitUntil: 'networkidle' });
    assert.match(await page.title(), /TG-Control/);
    await page.fill('#owner-token', process.env.TG_TEST_OWNER);
    await page.click('#login-form button[type=submit]');
    await page.waitForSelector('#app:not([hidden])');
    if (process.env.TG_SCREENSHOT) await page.screenshot({ path: process.env.TG_SCREENSHOT, fullPage: true });
    await page.click('[data-view=accounts]');
    const unsafeAlias = '<img src=x onerror="window.__tgXss=1">';
    await page.evaluate(() => { window.__tgXss = 0; });
    await page.fill('#account-alias', unsafeAlias);
    await page.fill('#account-phone', '+12025550100');
    await page.click('#account-form button[type=submit]');
    await page.waitForSelector('#account-list .list-row');
    assert.equal(await page.locator('#account-list img').count(), 0);
    assert.equal(await page.evaluate(() => window.__tgXss), 0);
    assert.match(await page.locator('#account-list').innerText(), /<img src=x/);
    await page.route('**/v1/admin/accounts/*/status', route => route.fulfill({ status: 200, contentType: 'application/json', body: '{"state":"reauth_required","intervention_required":true}' }));
    await page.click('#refresh');
    await page.waitForSelector('#session-alert:not([hidden])');
    assert.match(await page.locator('#session-alert').innerText(), /requires owner intervention/);
    await page.click('[data-view=clients]');
    assert.ok(await page.locator('#session-alert').isVisible());
    await page.unroute('**/v1/admin/accounts/*/status');
    await page.route('**/v1/admin/accounts/*/status', route => route.fulfill({ status: 200, contentType: 'application/json', body: '{"state":"disconnected","intervention_required":false}' }));
    await page.click('#refresh');
    await page.waitForFunction(() => document.querySelector('#session-alert').textContent.includes('disconnected'));
    await page.unroute('**/v1/admin/accounts/*/status');
    await page.route('**/v1/admin/accounts/*/status', route => route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":{"code":"unavailable"}}' }));
    await page.click('#refresh');
    await page.waitForFunction(() => document.querySelector('#session-alert').textContent.includes('Unable to verify'));
    await page.unroute('**/v1/admin/accounts/*/status');
    await page.click('#refresh');
    await page.waitForFunction(() => document.querySelector('#session-alert').hidden);

    await page.route('**/v1/admin/clients', async route => {
      if (route.request().method() === 'GET') await route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"test_failure"}' });
      else await route.continue();
    });
    await page.fill('#client-name', 'Browser synthetic client');
    await page.click('#client-form button[type=submit]');
    await page.waitForSelector('#credential-box:not([hidden])');
    const clientToken = await page.inputValue('#issued-credential');
    assert.ok(clientToken.startsWith('tgc_'));
    await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('refreshing workspace data failed'));
    assert.equal(await page.inputValue('#issued-credential'), clientToken);
    await page.unroute('**/v1/admin/clients');
    await page.click('#refresh');
    await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('Workspace refreshed'));
    await page.click('#dismiss-credential');
    assert.equal(await page.inputValue('#issued-credential'), '');
    await page.click('[data-view=policies]');
    const accountId = await page.locator('#policy-account option').nth(1).getAttribute('value');
    const clientId = await page.locator('#policy-client option').nth(1).getAttribute('value');
    await page.selectOption('#policy-account', accountId);
    await page.selectOption('#policy-client', clientId);
    await page.click('#load-policy');
    await page.waitForSelector('#policy-form:not([hidden])');
    await page.check('#read-enabled');
    await page.check('#read-channels');
    await page.click('#policy-form button[type=submit]');
    await page.waitForFunction(() => document.querySelector('#policy-state').textContent.includes('Policy saved'));
    const check = await page.evaluate(async ({ accountId, clientToken }) => {
      const call = async (operation, chat) => {
        const result = await fetch('/v1/execute', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${clientToken}` }, body: JSON.stringify({ actions: [{ operation, account_id: accountId, chat_id: chat }] }) });
        return result.status;
      };
      return [await call('history', 'public-channel'), await call('history', 'private-direct')];
    }, { accountId, clientToken });
    assert.deepEqual(check, [200, 403]);
    await page.click('[data-view=retention]');
    await page.fill('#audit-days', '45');
    await page.click('#retention-form button[type=submit]');
    await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('Retention settings saved'));
    await page.click('[data-view=audit]');
    assert.ok((await page.locator('#audit-list .audit-row').count()) > 0);
    // Revoked owner access locks and clears the rendered workspace.
    await page.route('**/v1/admin/accounts/*/status', route => route.fulfill({ status: 401, contentType: 'application/json', body: '{"detail":{"code":"invalid_credential"}}' }));
    await page.click('#refresh');
    await page.waitForSelector('#login:not([hidden])');
    assert.match(await page.locator('#login-error').innerText(), /no longer valid/);
    assert.equal(await page.locator('#account-list').innerText(), '');
    await page.unroute('**/v1/admin/accounts/*/status');
    await page.fill('#owner-token', process.env.TG_TEST_OWNER);
    await page.click('#login-form button[type=submit]');
    await page.waitForSelector('#app:not([hidden])');
    await page.setViewportSize({ width: 390, height: 844 });
    assert.ok(await page.locator('#logout').isVisible());
    let releaseAccounts, accountsRequested;
    const gate = new Promise(resolve => { releaseAccounts = resolve; });
    const requestSeen = new Promise(resolve => { accountsRequested = resolve; });
    await page.route('**/v1/admin/accounts', async route => {
      if (route.request().method() === 'GET') { accountsRequested(); await gate; }
      await route.continue();
    });
    await page.click('#refresh');
    await requestSeen;
    await page.click('#logout');
    const responseAfterLogout = page.waitForResponse(response => response.url().endsWith('/v1/admin/accounts'));
    releaseAccounts();
    await responseAfterLogout;
    await page.unroute('**/v1/admin/accounts');
    assert.ok(await page.locator('#login').isVisible());
    assert.equal(await page.inputValue('#owner-token'), '');
    assert.equal(await page.locator('#account-list .list-row').count(), 0);
    assert.equal(await page.locator('#audit-list .audit-row').count(), 0);
    await page.fill('#owner-token', process.env.TG_TEST_OWNER);
    await page.click('#login-form button[type=submit]');
    await page.waitForSelector('#app:not([hidden])');
    await page.evaluate(() => {
      const original = window.setTimeout;
      window.setTimeout = (fn, ms, ...args) => {
        if (ms === 15 * 60 * 1000) { window.__idleCallback = fn; return 99999; }
        return original(fn, ms, ...args);
      };
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'a' }));
      window.__idleCallback();
    });
    assert.ok(await page.locator('#login').isVisible());
    assert.match(await page.locator('#login-error').innerText(), /inactivity/);
    assert.equal(await page.locator('#account-list .list-row').count(), 0);
    assert.deepEqual(errors, []);
    console.log('Browser admin workflow passed.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error.stack); process.exit(1); });
