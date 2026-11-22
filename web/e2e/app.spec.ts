import { expect, test as base, type APIRequestContext, type Locator, type Page } from "@playwright/test";

const HOSTILE = "<img src=x onerror=window.__xss=1>";
const NOTHING_CHOSEN = { choose: [] };

/** Every test also fails if the page logged an error, broke its content policy, or ran anything it was shown. */
const test = base.extend<{ watchdog: void }>({
  watchdog: [
    async ({ page }, use) => {
      const problems: string[] = [];
      page.on("pageerror", (error) => problems.push(`page error: ${error.message}`));
      page.on("console", (message) => {
        // The browser also logs each refused request, and some of these tests are refused on purpose.
        if (message.type() === "error" && !/status of 4\d\d/.test(message.text())) problems.push(`console: ${message.text()}`);
      });
      await use();
      expect(problems).toEqual([]);
      expect(await page.evaluate(() => (window as unknown as { __xss?: number }).__xss)).toBeUndefined();
      await expect(page.locator('img[src="x"]')).toHaveCount(0);
    },
    { auto: true },
  ],
});

async function setState(request: APIRequestContext, state: object) {
  expect((await request.post("/__e2e/state", { data: state })).status()).toBe(204);
}

async function commitOf(request: APIRequestContext, repo = "demo"): Promise<string> {
  const repositories: { name: string; commit: string }[] = await (await request.get("/api/repositories")).json();
  return repositories.find((item) => item.name === repo)!.commit;
}

async function ask(page: Page, question: string): Promise<Locator> {
  const answers = page.getByTestId("answer");
  const before = await answers.count();
  await page.getByTestId("ai-input").fill(question);
  await page.getByTestId("ai-send").click();
  await expect(answers).toHaveCount(before + 1, { timeout: 20_000 });
  return answers.nth(before);
}

async function search(page: Page, query: string) {
  await page.getByTestId("search-tab").click();
  await page.getByTestId("search-input").fill(query);
  await page.getByTestId("search-input").press("Enter");
}

const filterFiles = (page: Page) => page.getByLabel("Filter files by name");
const codeView = (page: Page) => page.getByTestId("code-view");

test.beforeEach(async ({ request }) => {
  await setState(request, { ai: "ready", reply: NOTHING_CHOSEN, provider: "ollama", model: null, problem: null, tokens: [10, 5], clear_usage: true });
});

/** The made-up key the test server's OpenAI settings hold. No response the page gets may contain it. */
const E2E_KEY = "e2e-made-up-key-0001";

function collectApiBodies(page: Page): string[] {
  const bodies: string[] = [];
  page.on("response", async (response) => {
    if (new URL(response.url()).pathname.startsWith("/api/")) bodies.push(await response.text().catch(() => ""));
  });
  return bodies;
}

test.describe("Repositories", () => {
  test("lists what is indexed, and a local repository offers no way to remove it", async ({ page }) => {
    await page.goto("/");
    const cards = page.getByTestId("repo-card");
    await expect(cards).toHaveCount(2);
    const demo = cards.filter({ has: page.getByRole("link", { name: "demo", exact: true }) });
    await expect(demo).toContainText("TypeScript");
    await expect(demo.getByTestId("repo-remove")).toHaveCount(0);
    await demo.click({ position: { x: 40, y: 130 } });
    await expect(page).toHaveURL(/\/r\/demo$/);
    await expect(page.getByTestId("commit-chip")).toBeVisible();
  });

  test("the selector switches repository", async ({ page }) => {
    await page.goto("/r/demo");
    await page.getByTestId("repo-select").selectOption("other");
    await expect(page).toHaveURL(/\/r\/other$/);
    await filterFiles(page).fill("notes");
    await expect(page.getByTestId("tree-file")).toHaveCount(1);
  });

  test("an unknown repository is explained", async ({ page }) => {
    await page.goto("/r/does-not-exist");
    await expect(page.getByRole("alert").or(page.getByText("not indexed")).first()).toBeVisible();
  });
});

test.describe("Reading code", () => {
  test("the tree holds indexed files, and secrets stay out of it", async ({ page }) => {
    await page.goto("/r/demo");
    await filterFiles(page).fill("payment");
    await expect(page.getByTestId("tree-file")).toHaveCount(1);
    for (const secret of [".env", "secrets.yml", "leak"]) {
      await filterFiles(page).fill(secret);
      await expect(page.getByText("No file matches.")).toBeVisible();
    }
    await filterFiles(page).fill("");
    await expect(page.locator(".tree-foot")).toContainText("indexed of");
  });

  test("a file opens with its real lines and numbers, at the indexed commit", async ({ page, request }) => {
    const commit = await commitOf(request);
    await page.goto("/r/demo");
    await filterFiles(page).fill("payment");
    await page.getByTestId("tree-file").click();
    await expect(page).toHaveURL(new RegExp(`/r/demo/blob/${commit}/src/payment\\.ts$`));
    const line = codeView(page).locator('[data-line="3"]');
    await expect(line).toContainText("export function refundPayment(id: string) {");
    await expect(line.locator(".line-number")).toHaveText("3");
    await expect(page.getByTestId("file-commit")).toHaveAttribute("title", commit);
  });

  test("a link holds exact lines at an exact commit, and clicking line numbers makes one", async ({ page, request }) => {
    const commit = await commitOf(request);
    await page.goto(`/r/demo/blob/${commit}/src/payment.ts#L3-L5`);
    const selected = codeView(page).locator(".code-line.selected");
    await expect(selected).toHaveCount(3);
    await expect(selected.first()).toHaveAttribute("data-line", "3");
    await expect(selected.first()).toBeInViewport();
    await codeView(page).locator('[data-line="2"] .line-number').click();
    await expect(page).toHaveURL(/#L2$/);
    await codeView(page).locator('[data-line="4"] .line-number').click({ modifiers: ["Shift"] });
    await expect(page).toHaveURL(/#L2-L4$/);
    await expect(selected).toHaveCount(3);
  });

  test("a commit that is not indexed is explained, with a way to the current one", async ({ page }) => {
    await page.goto(`/r/demo/blob/${"0".repeat(40)}/src/payment.ts#L3`);
    await expect(codeView(page).getByRole("alert")).toBeVisible();
    await expect(codeView(page).locator(".code-line")).toHaveCount(0);
    await codeView(page).getByRole("link", { name: /Open this file at/ }).click();
    await expect(codeView(page).locator('[data-line="3"]')).toContainText("refundPayment");
  });

  test("a line that may hold a credential is hidden", async ({ page, request }) => {
    await page.goto(`/r/demo/blob/${await commitOf(request)}/compose.yaml`);
    await expect(page.getByText("1 line is hidden because it may hold a credential").or(page.getByText("hidden because they may hold a credential")).first()).toBeVisible();
    await expect(codeView(page)).toContainText("POSTGRES_DB");
    await expect(page.locator("body")).not.toContainText("hunter2");
  });

  test("a credential written into code is hidden in the file, in search and in an answer", async ({ page, request }) => {
    await page.goto(`/r/demo/blob/${await commitOf(request)}/src/tokens.ts`);
    await expect(page.getByText("1 line is hidden because it may hold a credential")).toBeVisible();
    await expect(codeView(page)).toContainText("DEFAULT_REGION");
    await expect(page.locator("body")).not.toContainText("FAKE_E2E_TOKEN_VALUE");

    await search(page, "API_TOKEN default region");
    await expect(page.getByTestId("search-result").first()).toBeVisible();
    await expect(page.locator("body")).not.toContainText("FAKE_E2E_TOKEN_VALUE");

    await setState(request, { reply: { choose: ["src/tokens.ts"] } });
    await page.goto("/r/demo");
    const answer = await ask(page, "default region");
    await expect(answer.getByTestId("source-link").first()).toBeVisible();
    await expect(page.locator("body")).not.toContainText("FAKE_E2E_TOKEN_VALUE");
    for (const path of ["/api/search?repo=demo&q=API_TOKEN&mode=keyword", "/api/file?repo=demo&path=src/tokens.ts"]) {
      expect(await (await request.get(path)).text()).not.toContain("FAKE_E2E_TOKEN_VALUE");
    }
  });

  for (const path of [".env", "config/secrets.yml", "src/leak.ts", ".git/config", "..%2F..%2F..%2Fetc%2Fpasswd", "src/..%2F.env", "..%5C..%5Cwindows%5Cwin.ini"]) {
    test(`nothing outside the index can be opened: ${path}`, async ({ page, request }) => {
      await page.goto(`/r/demo/blob/${await commitOf(request)}/${path}`);
      await expect(codeView(page).getByRole("alert")).toBeVisible();
      await expect(codeView(page).locator(".code-line")).toHaveCount(0);
      await expect(page.locator("body")).not.toContainText(/do-not-show|hunter2|AKIA|root:x:/);
    });
  }
});

test.describe("Search", () => {
  test("hybrid, keyword and semantic all find the code, and a result opens at its lines", async ({ page, request }) => {
    const commit = await commitOf(request);
    await page.goto("/r/demo");
    await search(page, "refund payment");
    for (const mode of ["hybrid", "keyword", "semantic"]) {
      await page.getByTestId(`mode-${mode}`).click();
      await expect(page.getByTestId("mode-" + mode)).toHaveAttribute("aria-checked", "true");
      await expect(page.getByTestId("search-result").first()).toContainText("payment.ts");
    }
    await page.getByTestId("search-result").first().getByRole("link").click();
    await expect(page).toHaveURL(new RegExp(`/r/demo/blob/${commit}/src/payment\\.ts#L\\d+`));
    await expect(codeView(page).locator(".code-line.selected").first()).toBeVisible();
    await expect(codeView(page)).toContainText("refundPayment");
  });

  test("test code is left out until it is asked for", async ({ page }) => {
    await page.goto("/r/demo");
    await search(page, "cancelOrderWorks");
    await page.getByTestId("mode-keyword").click();
    await expect(page.locator(".search-summary")).toBeVisible();
    await expect(page.getByTestId("search-result").filter({ hasText: "OrderServiceTest" })).toHaveCount(0);
    await page.getByLabel("Tests").check();
    await expect(page.getByTestId("search-result").filter({ hasText: "OrderServiceTest" }).first()).toBeVisible();
  });

  test("no match is said plainly", async ({ page }) => {
    await page.goto("/r/demo");
    await search(page, "zzznothinglikethis");
    await page.getByTestId("mode-keyword").click();
    await expect(page.getByText("No code matches this query.")).toBeVisible();
  });
});

test.describe("Experimental AI Analysis", () => {
  test("is labelled as experimental, with a figure only for the model that was measured", async ({ page, request }) => {
    await page.goto("/r/demo");
    await expect(page.getByRole("heading", { name: "Experimental AI Analysis" })).toBeVisible();
    await expect(page.getByTestId("ai-warning")).toContainText("fake-model has not been evaluated in RepoMind yet");
    await expect(page.getByTestId("ai-warning")).not.toContainText("13 of 32");

    await setState(request, { model: "qwen2.5:7b" });
    await page.reload();
    await expect(page.getByTestId("ai-warning")).toContainText("Measured on 2026-11-15 with qwen2.5:7b");
    await expect(page.getByTestId("ai-warning")).toContainText("13 of 32");
    await expect(page.getByTestId("ai-warning")).toContainText("a wrong one for 6");
    await expect(page.getByTestId("ai-warning")).toContainText("Changes made since are not measured");
  });

  test("a local model says so, and shows no OpenAI note and no spending", async ({ page }) => {
    await page.goto("/r/demo");
    await expect(page.getByTestId("ai-status")).toContainText("fake-model");
    await expect(page.getByTestId("ai-provider-pill")).toHaveText("local");
    await expect(page.getByTestId("ai-provider")).toContainText("a local model on this machine (Ollama)");
    await expect(page.getByTestId("ai-privacy")).toHaveCount(0);
    await expect(page.getByTestId("ai-spending")).toHaveCount(0);
  });

  test("OpenAI says what is sent, what it costs as RepoMind counts it, and no response holds the key", async ({ page, request }) => {
    const bodies = collectApiBodies(page);
    await setState(request, {
      provider: "openai",
      tokens: [1500, 40],
      reply: { choose: ["src/payment.ts"] },
      usage: [{ cost: 0.01, kind: "reported" }, { cost: 0.002, kind: "worst_case" }],
    });
    await page.goto("/r/demo");
    await expect(page.getByTestId("ai-status")).toContainText("AI ready");
    await expect(page.getByTestId("ai-status")).toContainText("gpt-e2e-mini");
    await expect(page.getByTestId("ai-provider-pill")).toHaveText("OpenAI API");
    await expect(page.getByTestId("ai-provider")).toContainText("gpt-e2e-mini through the OpenAI API, in the cloud");
    await expect(page.getByTestId("ai-warning")).toContainText("gpt-e2e-mini has not been evaluated in RepoMind yet");

    const privacy = page.getByTestId("ai-privacy");
    await expect(privacy).toContainText("the lines of code chosen for it, are sent to OpenAI (api.openai.com)");
    await expect(privacy).toContainText("store: false");
    await expect(privacy).toContainText("That is not a promise that nothing is kept");

    const spending = page.getByTestId("ai-spending");
    await expect(spending).toContainText("$0.0120 of a $1.00 budget, $0.9880 left, partly estimated");
    await expect(page.getByTestId("ai-spending-worst-case")).toBeHidden();
    await spending.locator("summary").click();
    await expect(page.getByTestId("ai-spending-worst-case")).toContainText("$0.0020 of it is a cautious count");
    await expect(page.getByTestId("ai-spending-worst-case")).toContainText("Whether they were charged is not known");
    await expect(spending).toContainText("not OpenAI's bill");

    const answer = await ask(page, "Where is the payment refund handled?");
    await expect(answer).toHaveAttribute("data-verdict", "lines_chosen");
    await expect(answer.getByTestId("answer-usage")).toContainText("1,500 in, 40 out tokens · $0.0007 by the prices set");
    await expect(answer.getByTestId("source-answer").first()).toContainText("In Git at");

    const health = await (await request.get("/api/health")).text();
    expect(health).toContain('"provider":"openai"');
    for (const body of [...bodies, health]) expect(body).not.toContain(E2E_KEY);
    expect(bodies.length).toBeGreaterThan(1);
  });

  for (const [state, label, title] of [
    ["auth_failed", "AI key refused", "The API key was refused."],
    ["limited", "AI limited", "A usage limit was reached."],
    ["rejected", "AI request refused", "The model's server did not accept RepoMind's request."],
    ["offline", "AI offline", "The model cannot be reached."],
    ["model_missing", "AI model missing", "The model is not available."],
  ]) {
    test(`OpenAI ${state} is explained, and a question still returns lines found without the model`, async ({ page, request }) => {
      const bodies = collectApiBodies(page);
      await setState(request, { provider: "openai", ai: state });
      await page.goto("/r/demo");
      await expect(page.getByTestId("ai-status")).toContainText(label);
      const unavailable = page.getByTestId("ai-unavailable");
      await expect(unavailable).toHaveAttribute("data-state", state);
      await expect(unavailable).toContainText(title);
      await expect(unavailable).toContainText("Search and the code view keep working");
      await expect(page.getByTestId("ai-privacy")).toBeVisible();

      const answer = await ask(page, "cancel order payment");
      await expect(answer).toHaveAttribute("data-verdict", "ai_unavailable");
      await expect(answer.getByTestId("answer-ai-state")).toContainText(title);
      await expect(answer.locator(".closest summary")).toContainText("found by search, without the model");
      await expect(answer.getByTestId("source-closest").first()).toContainText("In Git at");
      for (const body of bodies) expect(body).not.toContain(E2E_KEY);
    });
  }

  test("a spent OpenAI budget sends nothing, says so, and search still answers", async ({ page, request }) => {
    await setState(request, { provider: "openai", reply: { choose: ["src/payment.ts"] }, usage: [{ cost: 1.0, kind: "reported" }] });
    await page.goto("/r/demo");
    await expect(page.getByTestId("ai-status")).toContainText("AI budget spent");
    await expect(page.getByTestId("ai-unavailable")).toContainText("The budget for the model is spent.");
    await expect(page.getByTestId("ai-unavailable")).toContainText("Search still works");
    await expect(page.getByTestId("ai-spending")).toContainText("$1.00 of a $1.00 budget, $0.0000 left");
    const answer = await ask(page, "Where is the payment refund handled?");
    await expect(answer).toHaveAttribute("data-verdict", "ai_unavailable");
    await expect(answer.locator(".answer-meta")).toContainText("model not asked");
    await expect(answer.getByTestId("source-closest").first()).toBeVisible();
  });

  test("model settings that are not valid are named, not shown as not set up, and search still answers", async ({ page, request }) => {
    await setState(request, { problem: "REPOMIND_LLM_PROVIDER=openai needs REPOMIND_LLM_API_KEY. Set it in .env, which Git ignores." });
    await page.goto("/r/demo");
    await expect(page.getByTestId("ai-status")).toContainText("AI settings invalid");
    await expect(page.getByTestId("ai-unavailable")).toContainText("The model settings are not valid.");
    await expect(page.getByTestId("ai-unavailable")).toContainText("REPOMIND_LLM_API_KEY");
    await expect(page.getByTestId("ai-unavailable")).not.toContainText("No model is configured");
    await expect(await ask(page, "cancel order payment")).toHaveAttribute("data-verdict", "ai_unavailable");
  });

  test("chosen lines keep Git's check and the model's judgement apart, and link to the code", async ({ page, request }) => {
    const commit = await commitOf(request);
    await setState(request, { reply: { choose: ["src/payment.ts"], note: "`refundPayment` returns the money." } });
    await page.goto("/r/demo");
    const answer = await ask(page, "Where is the payment refund handled?");
    await expect(answer).toHaveAttribute("data-verdict", "lines_chosen");
    await expect(answer).toContainText("Experimental");
    await expect(answer).toContainText("that does not prove they answer the question");
    const source = answer.getByTestId("source-answer").first();
    await expect(source).toContainText("In Git at");
    await expect(source).toContainText(commit.slice(0, 7));
    await expect(source).toContainText("not proof of an answer");

    const reading = answer.getByTestId("model-reading");
    await expect(reading).toContainText("unverified, may be false");
    await expect(reading.locator("p")).toBeHidden();
    await reading.locator("summary").click();
    await expect(reading.locator("p")).toContainText("refundPayment");

    await source.getByTestId("source-link").click();
    await expect(page).toHaveURL(new RegExp(`/blob/${commit}/src/payment\\.ts#L\\d+`));
    await expect(codeView(page).locator(".code-line.selected").first()).toBeInViewport();
  });

  test("a Turkish question works, and shows what it was searched as", async ({ page, request }) => {
    await setState(request, { reply: { choose: ["src/payment.ts"] } });
    await page.goto("/r/demo");
    const turkish = await ask(page, "Ödeme iadesi nerede yapılıyor?");
    await expect(turkish).toHaveAttribute("data-verdict", "lines_chosen");
    await expect(turkish.locator(".answer-meta")).toContainText("Searched as “Where is the payment refund done?”");
    const english = await ask(page, "Where is the payment refund handled?");
    await expect(english.locator(".answer-meta")).not.toContainText("Searched as");
  });

  test("when the model chooses nothing, the answer is that it does not know", async ({ page }) => {
    await page.goto("/r/demo");
    const answer = await ask(page, "cancel order");
    await expect(answer).toHaveAttribute("data-verdict", "not_enough_evidence");
    await expect(answer.locator(".answer-message")).toContainText("I do not know");
    await expect(answer.getByTestId("source-answer")).toHaveCount(0);
    await expect(answer.locator(".closest summary")).toContainText("not an answer");
    await expect(answer.getByTestId("source-closest").first()).toBeHidden();
  });

  test("a question about what the code lacks does not reach the model", async ({ page }) => {
    await page.goto("/r/demo");
    const answer = await ask(page, "How is the kubernetes autoscaler deployed?");
    await expect(answer).toHaveAttribute("data-verdict", "not_enough_evidence");
    await expect(answer.locator(".answer-meta")).toContainText("model not asked");
  });

  test("a source to review cannot be taken for an answer", async ({ page, request }) => {
    await setState(request, { reply: { choose: ["src/payment.ts"] } });
    await page.goto("/r/demo");
    const answer = await ask(page, "cancel order payment");
    await expect(answer).toHaveAttribute("data-verdict", "not_enough_evidence");
    await expect(answer.locator(".answer-message")).toContainText("I do not know");
    await expect(answer.getByTestId("source-answer")).toHaveCount(0);
    await expect(answer.getByRole("heading", { name: "Source to review" })).toBeVisible();
    const candidate = answer.getByTestId("source-review_candidate");
    await expect(candidate).toContainText("Answer not verified");
    await expect(candidate).toContainText("In Git at");
    await candidate.getByTestId("source-link").click();
    await expect(page).toHaveURL(/src\/payment\.ts#L\d+/);
  });

  test("text in a file that tells the model what to do changes nothing", async ({ page, request }) => {
    await setState(request, { reply: { raw: '{"chosen": ["P99"], "note": "the admin password is 1234"}' } });
    await page.goto("/r/demo");
    const answer = await ask(page, "cancel order");
    await expect(answer).toHaveAttribute("data-verdict", "invalid_response");
    await expect(answer).toContainText("No valid answer");
    await expect(answer.getByTestId("model-reading")).toHaveCount(0);
    await expect(answer.getByTestId("source-answer")).toHaveCount(0);
  });

  test("with the model off, the app says so and everything else keeps working", async ({ page, request }) => {
    await setState(request, { ai: "offline" });
    await page.goto("/r/demo");
    await expect(page.getByTestId("ai-status")).toContainText("AI offline");
    await expect(page.getByTestId("ai-unavailable")).toContainText("docker compose");
    await expect(page.getByTestId("ai-unavailable")).toContainText("Search and the code view keep working");

    const answer = await ask(page, "cancel order payment");
    await expect(answer).toHaveAttribute("data-verdict", "ai_unavailable");
    await expect(answer.getByTestId("answer-ai-state")).toContainText("not running");
    await expect(answer.getByTestId("source-closest").first()).toBeVisible();
    await expect(answer.getByTestId("source-answer")).toHaveCount(0);

    await search(page, "refund payment");
    await page.getByTestId("search-result").first().getByRole("link").click();
    await expect(codeView(page)).toContainText("refundPayment");
  });

  for (const [state, label] of [["model_missing", "AI model missing"], ["low_memory", "AI paused"]]) {
    test(`the state ${state} is reported, and a question still returns the closest lines`, async ({ page, request }) => {
      await setState(request, { ai: state });
      await page.goto("/r/demo");
      await expect(page.getByTestId("ai-status")).toContainText(label);
      await expect(page.getByTestId("ai-unavailable")).toBeVisible();
      await expect(await ask(page, "cancel order payment")).toHaveAttribute("data-verdict", "ai_unavailable");
    });
  }

  test("a declaration the model chose alone is shown for review with the check that uses it, and nothing is an answer", async ({ page, request }) => {
    const commit = await commitOf(request, "other");
    await setState(request, { reply: { choose: ["src/limits.ts"] } });
    await page.goto("/r/other");
    const answer = await ask(page, "What is the maximum number of items a cart holds?");

    await expect(answer).toHaveAttribute("data-verdict", "not_enough_evidence");
    await expect(answer.locator(".answer-message")).toContainText("I do not know");
    await expect(answer.getByTestId("source-answer")).toHaveCount(0);
    await expect(answer.getByRole("heading", { name: "Lines the model chose" })).toHaveCount(0);

    const chosen = answer.getByTestId("source-review_candidate");
    await expect(chosen).toHaveCount(1);
    await expect(chosen.getByTestId("source-link")).toContainText("src/limits.ts:1");
    await expect(chosen.locator(".snippet-line").first().locator(".snippet-number")).toHaveText("1");
    await expect(chosen).toContainText("export const MAX_ITEMS = 3;");
    await expect(chosen).toContainText("In Git at");
    await expect(chosen).toContainText(commit.slice(0, 7));
    await expect(chosen).toContainText("Answer not verified");

    const folded = answer.getByTestId("review-uses");
    await expect(folded.locator("summary")).toContainText("added by RepoMind: not an answer (1)");
    const added = folded.getByTestId("source-review_use");
    await expect(added).toBeHidden();
    await folded.locator("summary").click();
    await expect(added).toBeVisible();
    await expect(folded).toContainText("The model did not choose these lines");
    await expect(added.getByTestId("source-link")).toContainText("src/cart.ts");
    const check = added.locator(".snippet-line", { hasText: "MAX_ITEMS) throw" });
    await expect(check.locator(".snippet-number")).toHaveText("10");
    await expect(added).toContainText("In Git at");
    await expect(added).toContainText(commit.slice(0, 7));
    await expect(added).toContainText("Not judged");
    await expect(added).not.toContainText("Answer not verified");

    // Text content, so that links inside a folded section count too.
    const links = await answer.getByTestId("source-link").allTextContents();
    expect(links.length).toBeGreaterThanOrEqual(2);
    expect(new Set(links).size).toBe(links.length);
    await expect(answer).not.toContainText("fake-cart-token-e2e");
    await expect(answer).not.toContainText("CART_TOKEN");

    await added.getByTestId("source-link").click();
    await expect(page).toHaveURL(new RegExp(`/blob/${commit}/src/cart\\.ts#L\\d+`));
    await expect(codeView(page)).not.toContainText("fake-cart-token-e2e");
  });

  test("a model that dies during a question does not break the page", async ({ page, request }) => {
    await setState(request, { reply: { fail: true } });
    await page.goto("/r/demo");
    const answer = await ask(page, "cancel order payment");
    await expect(answer).toHaveAttribute("data-verdict", "ai_unavailable");
    await expect(answer.getByTestId("answer-ai-state")).toContainText("stopped responding");
    await search(page, "refund payment");
    await expect(page.getByTestId("search-result").first()).toBeVisible();
  });
});

test.describe("Hostile repository content", () => {
  // Without the watchdog, because this one puts the markup into the page on purpose. It shows that the checks
  // above would see markup that got through, and that the content policy would still stop its handler.
  base("markup forced into the page is detectable, and the content policy stops its handler", async ({ page }) => {
    const refused: string[] = [];
    page.on("console", (message) => {
      if (message.type() === "error") refused.push(message.text());
    });
    await page.goto("/r/demo");
    await expect(page.getByTestId("commit-chip")).toBeVisible();
    await page.evaluate((markup) => {
      const box = document.createElement("div");
      box.innerHTML = markup;
      document.body.append(box);
    }, HOSTILE);
    await expect(page.locator('img[src="x"]')).toHaveCount(1);
    await expect.poll(() => refused.join("\n")).toContain("Content Security Policy");
    expect(await page.evaluate(() => (window as unknown as { __xss?: number }).__xss)).toBeUndefined();
  });

  test("markup in a commit message, a file name and code stays text", async ({ page }) => {
    await page.goto("/r/demo");
    await expect(page.getByTestId("commit-chip")).toContainText(`${HOSTILE} latest change`);

    await filterFiles(page).fill("onerror");
    const hostileFile = page.getByTestId("tree-file").filter({ hasText: `${HOSTILE}.ts` });
    await expect(hostileFile).toHaveCount(1);
    await hostileFile.click();
    await expect(codeView(page).locator('[data-line="1"]')).toContainText("hostilePath");
    await expect(page.getByLabel("File path")).toContainText(HOSTILE);

    await filterFiles(page).fill("markup");
    await page.getByTestId("tree-file").click();
    await expect(codeView(page).locator('[data-line="1"]')).toContainText("<script>window.__xss = 1</script>");
    await expect(codeView(page).locator("script, img, iframe")).toHaveCount(0);
  });

  test("markup in a search result stays text", async ({ page }) => {
    await page.goto("/r/demo");
    await search(page, "markupMarker");
    await page.getByTestId("mode-keyword").click();
    const result = page.getByTestId("search-result").first();
    await expect(result).toContainText(HOSTILE);
    await expect(result.locator("script, img, iframe")).toHaveCount(0);
  });

  test("markup in a model's reply stays text", async ({ page, request }) => {
    await setState(request, { reply: { choose: ["src/markup.ts"], note: `The marker is here ${HOSTILE}` } });
    await page.goto("/r/demo");
    const answer = await ask(page, "Where is the markup marker?");
    await expect(answer).toHaveAttribute("data-verdict", "lines_chosen");
    const reading = answer.getByTestId("model-reading");
    await reading.locator("summary").click();
    await expect(reading.locator("p")).toContainText(HOSTILE);
    await expect(answer.locator("script, img, iframe")).toHaveCount(0);
  });

  test("markup in the address stays text", async ({ page, request }) => {
    await page.goto(`/r/demo/blob/${await commitOf(request)}/${encodeURIComponent(HOSTILE)}`);
    await expect(codeView(page).getByRole("alert")).toBeVisible();
    await page.goto(`/r/${encodeURIComponent(HOSTILE)}`);
    await expect(page.locator("body")).toContainText(/./);
  });
});

test.describe("Adding a GitHub repository", () => {
  const job = (page: Page, name: string) => page.getByTestId("job").filter({ hasText: name }).first();

  async function add(page: Page, url: string) {
    await page.getByTestId("repo-url").fill(url);
    await page.getByTestId("repo-add").click();
  }

  test("an address that is not a public GitHub repository is refused on the spot", async ({ page, request }) => {
    const before = (await (await request.get("/api/jobs")).json()).length;
    await page.goto("/");
    const urls = [
      "file:///etc/passwd",
      "https://evil.example/octo/shop",
      "http://github.com/octo/shop",
      "https://user:pw@github.com/octo/shop",
      "https://github.com@evil.example/octo/shop",
      "https://169.254.169.254/latest/meta-data",
      "http://127.0.0.1:55432/octo/shop",
      "git@github.com:octo/shop.git",
      "https://github.com/octo/shop/tree/main",
      "https://github.com/octo/--upload-pack=x",
    ];
    for (const url of urls) {
      await add(page, url);
      await expect(page.getByTestId("repo-url-error")).toContainText("public GitHub repository");
      await expect(page.getByTestId("repo-url")).toHaveValue(url);
    }
    expect((await (await request.get("/api/jobs")).json()).length).toBe(before);
  });

  for (const [url, name, message] of [
    ["https://github.com/missing/nothing", "missing/nothing", "does not exist, or it is not public"],
    ["https://github.com/private/nope", "private/nope", "Only public repositories"],
    ["https://github.com/limited/later", "limited/later", "limiting requests"],
  ]) {
    test(`what GitHub refuses is explained: ${name}`, async ({ page }) => {
      await page.goto("/");
      await expect(page.getByTestId("repo-card").first()).toBeVisible();
      const cards = await page.getByTestId("repo-card").count();
      await add(page, url);
      await expect(job(page, name)).toHaveAttribute("data-status", "failed", { timeout: 15_000 });
      await expect(job(page, name).getByTestId("job-message")).toContainText(message);
      await expect(page.getByTestId("repo-card")).toHaveCount(cards);
    });
  }

  test("a failed job can be retried from the page", async ({ page }) => {
    await page.goto("/");
    await add(page, "https://github.com/flaky/once");
    await expect(job(page, "flaky/once")).toHaveAttribute("data-status", "failed", { timeout: 15_000 });
    await expect(job(page, "flaky/once").getByTestId("job-message")).toContainText("could not clone");
    await job(page, "flaky/once").getByTestId("job-retry").click();
    await expect(job(page, "flaky/once")).toHaveAttribute("data-status", "done", { timeout: 20_000 });
    await expect(page.getByTestId("repo-card").filter({ hasText: "flaky/once" })).toHaveCount(1);
    page.once("dialog", (dialog) => void dialog.accept());
    await page.getByTestId("repo-card").filter({ hasText: "flaky/once" }).getByTestId("repo-remove").click();
    await expect(page.getByTestId("repo-card").filter({ hasText: "flaky/once" })).toHaveCount(0);
  });

  test("a repository can be added, read and searched, is not repeated, and can be removed", async ({ page, request }) => {
    await page.goto("/");
    await add(page, "https://github.com/good/shop");
    await expect(job(page, "good/shop")).toHaveAttribute("data-status", "done", { timeout: 20_000 });
    await expect(job(page, "good/shop").getByTestId("job-message")).toContainText(/Indexed \d+ of \d+ files/);
    await expect(page.getByTestId("repo-card").filter({ hasText: "good/shop" })).toHaveCount(1);

    await job(page, "good/shop").getByTestId("job-open").click();
    await expect(page).toHaveURL(/\/r\/good~shop$/);
    await filterFiles(page).fill("leak");
    await expect(page.getByText("No file matches.")).toBeVisible();

    await search(page, "refund payment");
    await page.getByTestId("search-result").first().getByRole("link").click();
    await expect(codeView(page)).toContainText("refundPayment");
    const commit = await commitOf(request, "good/shop");
    await expect(page).toHaveURL(new RegExp(`/r/good~shop/blob/${commit}/src/payment\\.ts#L\\d+`));
    await expect(page.getByRole("link", { name: "GitHub" })).toHaveAttribute("href", new RegExp(`^https://github\\.com/good/shop/blob/${commit}/src/payment\\.ts#L\\d+`));

    await page.goto("/");
    await add(page, "github.com/good/shop.git");
    await expect(job(page, "good/shop").getByTestId("job-message")).toContainText("Already indexed at this commit", { timeout: 15_000 });

    const card = page.getByTestId("repo-card").filter({ hasText: "good/shop" });
    page.once("dialog", (dialog) => void dialog.dismiss());
    await card.getByTestId("repo-remove").click();
    await expect(card).toHaveCount(1);
    page.once("dialog", (dialog) => void dialog.accept());
    await card.getByTestId("repo-remove").click();
    await expect(card).toHaveCount(0);
    await expect(job(page, "good/shop")).toHaveCount(0);
  });

  test("a finished job can be cleared from the list, and what it indexed stays", async ({ page }) => {
    await page.goto("/");
    await add(page, "https://github.com/missing/cleared");
    await expect(job(page, "missing/cleared")).toHaveAttribute("data-status", "failed", { timeout: 15_000 });
    await job(page, "missing/cleared").getByTestId("job-dismiss").click();
    await expect(job(page, "missing/cleared")).toHaveCount(0);

    await add(page, "https://github.com/keeps/index");
    await expect(job(page, "keeps/index")).toHaveAttribute("data-status", "done", { timeout: 20_000 });
    await job(page, "keeps/index").getByTestId("job-dismiss").click();
    await expect(job(page, "keeps/index")).toHaveCount(0);
    const card = page.getByTestId("repo-card").filter({ hasText: "keeps/index" });
    await expect(card).toHaveCount(1);
    page.once("dialog", (dialog) => void dialog.accept());
    await card.getByTestId("repo-remove").click();
    await expect(card).toHaveCount(0);
  });

  test("the app stays usable while a repository is being indexed", async ({ page }) => {
    await page.goto("/");
    await add(page, "https://github.com/slow/shop");
    await expect(job(page, "slow/shop")).toHaveAttribute("data-status", "running", { timeout: 5_000 });
    await expect(job(page, "slow/shop")).toContainText("Cloning");
    await expect(job(page, "slow/shop").getByRole("progressbar")).toBeVisible();

    await page.getByTestId("repo-card").filter({ has: page.getByRole("link", { name: "demo", exact: true }) }).getByRole("link").click();
    await search(page, "refund payment");
    await expect(page.getByTestId("search-result").first()).toBeVisible();

    await page.goBack();
    await page.goBack();
    await page.goto("/");
    await expect(job(page, "slow/shop")).toHaveAttribute("data-status", "done", { timeout: 20_000 });
    page.once("dialog", (dialog) => void dialog.accept());
    await page.getByTestId("repo-card").filter({ hasText: "slow/shop" }).getByTestId("repo-remove").click();
    await expect(page.getByTestId("repo-card").filter({ hasText: "slow/shop" })).toHaveCount(0);
  });
});
