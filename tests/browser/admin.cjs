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
    const monitorAccountId = await page.locator('#policy-account option').nth(1).getAttribute('value');
    const sessionHistoryRoute = /\/v1\/admin\/audit\?event=session_transition&limit=5$/;
    const monitorRoute = '**/v1/admin/session-monitor';
    await page.route(monitorRoute, route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ task_running: true, pass_in_progress: false, last_result: 'failure', consecutive_failures: 1, last_success_at: unsafeAlias, current_pass_seconds: null }) }));
    await page.route(sessionHistoryRoute, route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ events: [{ account_id: monitorAccountId, decision: 'reauth_required', timestamp: 'synthetic-time' }] }) }));
    await page.route('**/v1/admin/accounts/*/status', route => route.fulfill({ status: 200, contentType: 'application/json', body: '{"state":"reauth_required","intervention_required":true}' }));
    await page.click('#refresh');
    await page.waitForSelector('#session-alert:not([hidden])');
    assert.match(await page.locator('#session-alert').innerText(), /requires owner intervention/);
    await page.waitForSelector('#session-events .session-event', { state: 'attached' });
    assert.match(await page.locator('#session-events').textContent(), /<img src=x/);
    assert.match(await page.locator('#session-events').textContent(), /Owner action required/);
    assert.equal(await page.locator('#session-events img').count(), 0);
    assert.equal(await page.evaluate(() => window.__tgXss), 0);
    await page.waitForSelector('#monitor-alert:not([hidden])');
    assert.match(await page.locator('#monitor-alert').innerText(), /failed its last pass/);
    assert.match(await page.locator('#monitor-summary').textContent(), /<img src=x/);
    assert.equal(await page.locator('#monitor-summary img').count(), 0);
    assert.equal(await page.evaluate(() => window.__tgXss), 0);
    await page.unroute(sessionHistoryRoute);
    await page.click('[data-view=clients]');
    assert.ok(await page.locator('#session-alert').isVisible());
    assert.ok(await page.locator('#monitor-alert').isVisible());
    await page.unroute(monitorRoute);
    await page.route(monitorRoute, route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ task_running: false, pass_in_progress: false, last_result: 'success', consecutive_failures: 0, last_success_at: 'synthetic-time', current_pass_seconds: null }) }));
    await page.click('#refresh');
    await page.waitForFunction(() => document.querySelector('#monitor-alert').textContent.includes('has stopped'));
    await page.unroute(monitorRoute);
    await page.route(monitorRoute, route => route.fulfill({ status: 200, contentType: 'application/json', body: '{"task_running":true,"last_result":"success"}' }));
    await page.click('#refresh');
    await page.waitForFunction(() => document.querySelector('#monitor-alert').textContent.includes('Unable to verify'));
    await page.unroute(monitorRoute);
    // An older successful poll must not hide a newer monitor failure.
    let releaseOldMonitor, oldMonitorRequested, monitorCalls = 0;
    const oldMonitorGate = new Promise(resolve => { releaseOldMonitor = resolve; });
    const oldMonitorSeen = new Promise(resolve => { oldMonitorRequested = resolve; });
    await page.route(monitorRoute, async route => {
      const call = ++monitorCalls;
      if (call === 1) { oldMonitorRequested(); await oldMonitorGate; }
      const result = call === 1
        ? { task_running: true, pass_in_progress: false, last_result: 'success', consecutive_failures: 0, last_success_at: 'old-success', current_pass_seconds: null }
        : { task_running: true, pass_in_progress: false, last_result: 'failure', consecutive_failures: 1, last_success_at: null, current_pass_seconds: null };
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(result) });
    });
    await page.click('#refresh');
    await oldMonitorSeen;
    await page.click('#refresh');
    await page.waitForFunction(() => document.querySelector('#monitor-alert').textContent.includes('failed its last pass'));
    await page.evaluate(() => { document.querySelector('#notice').textContent = 'Waiting for earlier refresh'; });
    releaseOldMonitor();
    await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('Workspace refreshed'));
    assert.match(await page.locator('#monitor-alert').innerText(), /failed its last pass/);
    await page.unroute(monitorRoute);
    // An older workspace load must not replace newer account data either.
    let releaseOldAccounts, oldAccountsRequested, accountCalls = 0;
    const oldAccountsGate = new Promise(resolve => { releaseOldAccounts = resolve; });
    const oldAccountsSeen = new Promise(resolve => { oldAccountsRequested = resolve; });
    await page.route('**/v1/admin/accounts', async route => {
      const call = ++accountCalls;
      if (call === 1) { oldAccountsRequested(); await oldAccountsGate; await route.fulfill({ status: 200, contentType: 'application/json', body: '{"accounts":[]}' }); }
      else await route.continue();
    });
    await page.click('#refresh');
    await oldAccountsSeen;
    await page.evaluate(() => { document.querySelector('#notice').textContent = 'Waiting for newer workspace'; });
    await page.click('#refresh');
    await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('Workspace refreshed'));
    await page.evaluate(() => { document.querySelector('#notice').textContent = 'Waiting for earlier workspace'; });
    releaseOldAccounts();
    await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('Workspace refreshed'));
    assert.equal(await page.locator('#account-list .list-row').count(), 1);
    assert.match(await page.locator('#account-list').textContent(), /<img src=x/);
    await page.unroute('**/v1/admin/accounts');
    await page.unroute('**/v1/admin/accounts/*/status');
    await page.route('**/v1/admin/accounts/*/status', route => route.fulfill({ status: 200, contentType: 'application/json', body: '{"state":"disconnected","intervention_required":false}' }));
    await page.click('#refresh');
    await page.waitForFunction(() => document.querySelector('#session-alert').textContent.includes('disconnected'));
    await page.waitForFunction(() => document.querySelector('#monitor-alert').hidden);
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
    await page.evaluate(() => { const marker = document.createElement('span'); marker.className = 'session-event'; marker.textContent = 'synthetic session detail'; document.querySelector('#session-events').append(marker); });
    await page.click('#logout');
    const responseAfterLogout = page.waitForResponse(response => response.url().endsWith('/v1/admin/accounts'));
    releaseAccounts();
    await responseAfterLogout;
    await page.unroute('**/v1/admin/accounts');
    assert.ok(await page.locator('#login').isVisible());
    assert.equal(await page.inputValue('#owner-token'), '');
    assert.equal(await page.locator('#account-list .list-row').count(), 0);
    assert.equal(await page.locator('#audit-list .audit-row').count(), 0);
    assert.equal(await page.locator('#session-events .session-event').count(), 0);
    assert.equal(await page.locator('#monitor-summary').textContent(), '');
    assert.equal(await page.locator('#monitor-alert').textContent(), '');
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
