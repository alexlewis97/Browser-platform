# Browser Platform Chrome extension

Manifest V3 extension that lists private marketplace automations and replays **workflow-use** steps in your current Chrome tab. No LLM, no Pomerado, and no local backend is required to run an already-installed workflow.

## Install (Load unpacked)

Chrome Web Store publishing is not done yet. For now:

1. From this repo, run `cd extension && npm install && npm run build`, then use the generated `extension/dist` folder (it is gitignored).
2. Open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, and select the unzipped folder (the one that contains `manifest.json`).
3. Pin the extension and click its icon to open the side panel.
4. In **Settings**, paste the same shared access token you use on https://browser-platform-web.vercel.app if you want to browse, install, or publish marketplace listings.
5. Open the **Market** tab to browse listings or publish: pick an installed/imported workflow from the Publish dropdown and tap **Publish** (or **Update** if it is already listed). Name, description, and inputs are filled in and can be edited. Learned HITL aliases travel with the workflow.
6. Open the website tab you want to automate, go to **Installed** (or import a workflow JSON), fill the required inputs, and press **Run in current tab**.

Traces appear in the side panel. If a fuzzy match is uncertain, pick a candidate there; the selector is saved on that installed workflow for the next run.

## Develop

```bash
cd extension
npm install
npm run build
npm test          # unit tests (normalize, score, inputs, selectors)
npx playwright test   # headed Chromium, unpacked extension + fixtures
```

Playwright tests serve `fixtures/attendance.html` locally and also hit live Wikipedia.
