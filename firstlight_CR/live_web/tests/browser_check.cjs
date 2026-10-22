const {chromium} = require('playwright');
const assert = require('node:assert/strict');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.CR_LIVE_TEST_BROWSER || undefined});
  const base = process.env.CR_LIVE_TEST_URL || 'http://127.0.0.1:8765/live/';
  try {
    const page = await browser.newPage({viewport:{width:1440,height:960}});
    const errors = [];
    page.on('pageerror', error=>errors.push(error.message));
    await page.goto(base);
    await page.locator('#start').waitFor({state:'visible'});
    await page.waitForFunction(()=>!document.getElementById('start').disabled);
    await page.waitForFunction(()=>document.getElementById('screen').naturalWidth > 0);
    const options = await page.locator('#checkpoint option').allTextContents();
    assert(options.includes('RL 711') && options.includes('RL 70'));
    await page.locator('#checkpoint').selectOption({label:'RL 70'});
    assert.equal(await page.locator('#weight-version').textContent(),'v2');
    await page.locator('#checkpoint').selectOption({label:'RL 711'});
    assert.equal(await page.locator('#weight-version').textContent(),'v3');
    await page.getByRole('button',{name:'添加本机权重',exact:true}).click();
    await page.locator('#weight-path').fill('/tmp/missing-live-checkpoint.pt');
    await page.getByRole('button',{name:'校验并添加'}).click();
    await page.waitForFunction(()=>document.getElementById('weight-error').textContent.length>0);
    await page.getByRole('button',{name:'关闭',exact:true}).click();
    const output = path.resolve('artifacts/live-web');
    for (const [name,width,height] of [['desktop',1440,960],['tablet',768,1024],['mobile',390,844]]) {
      await page.setViewportSize({width,height});
      const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth);
      assert.equal(overflow,false,`${name} horizontal overflow`);
      await page.screenshot({path:path.join(output,`ui-${name}.png`),fullPage:true});
    }
    await page.getByRole('button',{name:/对局记录/}).click();
    assert(await page.locator('#history-view').isVisible());
    await page.getByRole('button',{name:'实时对战',exact:true}).click();
    await page.setViewportSize({width:1440,height:960});
    if (process.argv.includes('--match')) {
      await page.locator('#match-mode').selectOption('standard');
      await page.getByRole('button',{name:'开始一局',exact:true}).click();
      await page.waitForFunction(()=>document.getElementById('stop').disabled===false);
      assert(await page.locator('#start').isDisabled());
      await page.waitForFunction(()=>['对战中','异常'].includes(document.getElementById('state-badge').textContent),{},{timeout:90000});
      assert.equal(await page.locator('#state-badge').textContent(),'对战中',await page.locator('#run-message').textContent());
      await page.screenshot({path:path.join(output,'ui-playing.png'),fullPage:true});
    }
    if (process.argv.includes('--stop')) {
      await page.locator('#match-mode').selectOption('standard');
      await page.getByRole('button',{name:'开始一局',exact:true}).click();
      await page.waitForFunction(()=>document.getElementById('stop').disabled===false);
      await page.getByRole('button',{name:'停止接管',exact:true}).click();
      await page.waitForFunction(()=>document.getElementById('state-badge').textContent==='已停止',{},{timeout:30000});
      const status=await page.request.get(new URL('api/status',base).href);
      const data=await status.json();
      assert.equal(data.busy,false);
      assert.equal(data.job.counts.confirmed,0);
      assert.equal(data.job.events.some(e=>e.event==='bound'),false);
    }
    if (base.includes('/live/')) {
      const localResponse = await page.request.get(new URL('api/checkpoints',base).href);
      const local = await localResponse.json();
      const remote = local.items.find(c=>c.source==='remote' && c.available && c.supported);
      assert(remote,'Remote checkpoint catalog is empty');
      await page.locator('#weight-search').fill(String(remote.iteration));
      await page.locator('#checkpoint').selectOption(remote.id);
      assert((await page.locator('#weight-source').textContent()).includes('远端'));
      await page.getByRole('link',{name:'对局视频',exact:true}).click();
      await page.locator('#top option').first().waitFor({state:'attached'});
      assert(await page.locator(`#top option[value="${remote.id}"]`).count());
      await page.getByRole('link',{name:'私服对战',exact:true}).click();
      await page.waitForFunction(()=>document.getElementById('checkpoint').value.length>0);
      await page.locator('#checkpoint').selectOption(remote.id);
      await page.waitForFunction(()=>document.getElementById('screen').naturalWidth>0 && document.getElementById('screen-message').hidden);
      await page.screenshot({path:path.join(output,'ui-unified.png'),fullPage:true});
      await page.goto('http://127.0.0.1:8770/');
      assert(page.url().includes('/live/'));
    }
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({optionCount:options.length,errors,matchStarted:process.argv.includes('--match')}));
  } finally {
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
