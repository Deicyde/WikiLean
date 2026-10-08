import { expect, test } from '@playwright/test';
test('native editor updates one retained link and preserves other occurrences through the real save path', async ({ page }) => {
  const errors: string[]=[];page.on('pageerror', error=>errors.push(error.message));
  await page.route('**/*', route => {
    const url=new URL(route.request().url());
    if(!['localhost','127.0.0.1'].includes(url.hostname)) throw new Error('Unexpected external request '+url.href);
    return route.continue();
  });
  await page.goto('/login?returnTo=%2FNative_Article');
  await page.evaluate(()=>localStorage.setItem('wl_editor_intro_seen','1'));
  await page.reload();
  await expect(page.locator('#wlr-orphans')).toContainText('locations not displayed');
  await page.locator('.anno').first().click();
  await expect(page.locator('#wlr-companions')).toBeVisible();
  await expect(page.locator('#wlr-legacy-formal')).toBeHidden();
  await page.locator('#wlr-f-occurrence').selectOption('link:pdf-companion');
  await expect(page.locator('#wlr-f-target')).toHaveValue('../lean.html#External');
  await expect(page.locator('#wlr-f-anchor-snippet')).toBeDisabled();
  await page.locator('#wlr-f-target').fill('urn:formal:changed');
  const saved=page.waitForResponse(response=>response.url().includes('/api/article/Native_Article') && response.request().method()==='POST');
  await page.getByRole('button',{name:'Save',exact:true}).click();
  const result=await saved;expect(result.status()).toBe(200);expect(await result.json()).toMatchObject({ok:true});
  const records=await (await page.request.get('/api/article/Native_Article/companion-records.json')).json();
  expect(records.records).toHaveLength(2);
  const external=records.records.find((record:any)=>record.id==='pdf-companion');
  expect(external).toMatchObject({base_uri:'https://example.net/annotations/group.json',annotation:{
    source:{type:'pdf',uri:'../group.pdf',extensions:{publisher:{retained:true}}},
    target:'urn:formal:changed',location:{page:2},extensions:{custom:{nested:[1,'keep',null]}}
  }});
  const migrated=records.records.find((record:any)=>record.id!=='pdf-companion').annotation;
  expect(migrated.target.uri).toContain('#AddCommGroup');
  expect(migrated.target.pin).toMatchObject({type:'repository',declaration:'AddCommGroup'});
  expect(migrated).toMatchObject({status:'formalized',source:{pin:{type:'website',revision:'12345'}}});
  expect(errors).toEqual([]);
});

test('selecting the second targetless location after linking the first keeps the intended source', async ({page})=>{
  await page.goto('/login?returnTo=%2FNative_Article');
  await page.evaluate(()=>localStorage.setItem('wl_editor_intro_seen','1'));
  await page.reload();
  await page.locator('.anno[data-anno-indices="1"]').click();
  await page.locator('#wlr-f-target').fill('urn:formal:first-selection');
  await page.locator('#wlr-f-occurrence').selectOption('unlinked:1');
  await expect(page.locator('#wlr-source-info')).toContainText('https://example.net/narrative.html');
  await expect(page.locator('#wlr-f-target')).toHaveValue('');
  await page.locator('#wlr-f-target').fill('urn:formal:second-selection');
  const saved=page.waitForResponse(response=>response.url().includes('/api/article/Native_Article') && response.request().method()==='POST');
  await page.getByRole('button',{name:'Save',exact:true}).click();
  const response=await saved;expect(response.status()).toBe(200);
  const data=await (await page.request.get('/api/article/Native_Article.json')).json();
  const item=data.annotations.find((value:any)=>value && value.id==='bbbbbbbbbbbb');
  expect(item.links).toHaveLength(2);expect(item.unlinked).toHaveLength(0);
  expect(item.links[0].annotation.target).toBe('urn:formal:first-selection');
  expect(item.links[1].annotation).toMatchObject({source:{type:'html',uri:'https://example.net/narrative.html'},target:'urn:formal:second-selection',location:{fragment:'result'}});
});
