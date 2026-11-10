---
name: opencart-extension
description: Use when writing, modifying, or reviewing OpenCart 3.x extension code of any type (module, payment, shipping, feed, total, dashboard) — controllers, models, language files, Twig views, install/uninstall lifecycle, event handlers, DB queries, config management, or ocmod XML modifications.
---

# OpenCart Extension Development
Targets OpenCart 3.0.2.0 to 3.0.5.x on **PHP 7.4+**. Every statement about core below holds across that range unless a version is named.

**Ask who the extension is credited to before writing any file** — the `@author` name and `@link` URL. Every PHP and JS file carries it and `install.xml` repeats it, so a wrong guess means rewriting everything. Examples below use `Your Name` and `https://github.com/username` as placeholders; the link may equally be `https://website.com/` or a `mailto:`. Never carry a byline over from another project.

## Non-negotiables
| Rule | § |
|---|---|
| Almost no comments. Never narrate, explain a call, or label a block | 13 |
| PHP 7.4: nothing from PHP 8 — no `match`, `?->`, named arguments, union types, `str_contains()` | 11 |
| Every property is typed. A closure whose body is one expression is `fn`, with no `use` | 4, 11 |
| No `use` imports in a namespaced file — fully qualify at point of use, **return types included** | 11 |
| No `@version` in a docblock | 3 |
| Integers are never quoted in SQL | 6 |
| Never assign a language key into `$data` for your own template | 8 |
| Always namespace `load->language()` in code core runs inside another page | 8 |
| Event handlers return nothing | 4 |
| `install()` grants the current user group `access` and `modify` on its own route, first | 5 |
| Request input arrives HTML-escaped: decode it before it leaves for an API, a hash or a file | 9 |
| `url->link()` returns `&amp;`: decode it before JSON, JavaScript or a header | 9 |
| No abbreviations, in any name | 11 |
| Align `key => value` in array literals; nothing else, ever | 11 |
| Every async JS call is awaited, returned, or `void` | 14 |
| Every `input` and `textarea` has a `placeholder`, defaulting to its label | 11 |
| Loader calls stack by type only; a blank line closes each block, including before the first model call | 11 |

**Paired extensions** — the same code shipped under two names — take every change in both, in the same edit. Diff the trees afterwards with the naming normalised; only credits may differ. A fix applied to one and forgotten in the other is worse than no fix, because the two drift silently.

---

## 1. Structure
```
upload/
  admin/
    controller/extension/{type}/{name}.php
    model/extension/{type}/{name}.php
    language/{en-gb,el-gr}/extension/{type}/{name}.php
    view/template/extension/{type}/{name}.twig
    view/stylesheet/{name}.css
    view/javascript/{name}.js
    view/image/payment/{name}.png               payment logo
  catalog/
    controller/extension/{type}/{name}.php      storefront side; omit if there is none
    model/extension/{type}/{name}.php
    language/{en-gb,el-gr}/extension/{type}/{name}.php
    view/theme/default/template/extension/{type}/{name}.twig
    view/theme/default/stylesheet/{name}.css
  system/library/{name}.php                     complex integrations only
install.xml
```

`{type}` is `module`, `shipping`, `feed`, `payment`… `{name}` is snake_case. Admin-only extensions omit `catalog/` entirely. The catalog controller is the storefront half — a payment form and its callbacks, a module's output, a feed, event handlers.

**The installer writes only under** `admin/controller/extension/`, `admin/model/extension/`, `admin/language/`, `admin/view/image/`, `admin/view/javascript/`, `admin/view/stylesheet/`, `admin/view/template/extension/`, `catalog/controller/extension/`, `catalog/model/extension/`, `catalog/language/`, `catalog/view/javascript/`, `catalog/view/theme/`, `system/config/`, `system/library/` and `image/catalog/`. A file anywhere else fails the whole upload.

**Per type.** What only one type of extension needs lives in its own file beside this one. Read it before touching an extension of that type:

| Type | File | Covers |
|---|---|---|
| `payment` | [references/payment.md](references/payment.md) | the checkout contract, the tab on the admin order page |

**Assets.** Anything beyond a few lines gets its own file named after the extension, always served with a version query — `self::VERSION`, or the library's `version()` when the extension ships one:

```php
$this->document->addStyle('view/stylesheet/{name}.css?v=' . self::VERSION);
```
```twig
<script type="text/javascript" src="view/javascript/{name}.js?v={{ version }}"></script>
```

Never inline a stylesheet; never omit the version. Page glue needing Twig values stays in a `<script>` at the foot of that template.

**Splitting.** When a settings page grows tool tabs, each tab becomes its own controller in a subfolder, with matching language and template folders:

```
controller/extension/module/{name}/url.php   → ControllerExtensionModule{Name}Url
language/en-gb/extension/module/{name}/url.php
view/template/extension/module/{name}/url.twig
```

A leaf is a controller in its own right — own `ROUTE`, own `json()` — not a partial. Core checks access on the first three parts of an extension route, so `extension/module/{name}/url` is guarded by `extension/module/{name}`, and nothing ever grants the leaf's own route. Every `hasPermission()` in a leaf checks the parent, held in a `MODULE` constant:

```php
private const ROUTE = 'extension/module/example/url';
private const MODULE = 'extension/module/example';
```

The parent maps tabs to leaves and loads each language into its own namespace, so `{{ urls.get('column_keyword') }}` cannot collide:

```php
private const TABS = ['urls' => 'url', 'missing' => 'missing', 'redirects' => 'redirect'];

foreach (self::TABS as $tab => $route) {
    $this->load->language(self::ROUTE . '/' . $route, $tab);
}
```

Split a tab with its own actions and table; do not split a fieldset.

---

## 2. Constants
```php
private const VERSION = '1.0';
private const ROUTE = 'extension/module/example';
private const CODE = 'module_example';
private const ERRORS = ['warning', 'title'];
private const FIELDS = ['status', 'title', 'api_key'];
private const EVENTS = [
    'admin/view/common/column_left/before' => self::ROUTE . '/viewCommonColumnLeftBefore',
    'admin/view/sale/order_info/after'     => self::ROUTE . '/viewSaleOrderInfoAfter',
];
```

Always `private const`. `CODE` is `{type}_{name}` and matches `install.xml` `<code>`; `ROUTE` matches the file path. Reference as `self::X`, never by class name. `FIELDS` drives the settings loop; `ERRORS` drives the error loop. `VERSION` matches `install.xml` `<version>`.

Never hold a model property name in a constant. Write it out — `$this->model_extension_module_example` — so the `@property` type reaches the IDE; `$this->{self::MODEL}` hides it at every call site.

---

## 3. Class declaration
```php
<?php

/**
 * Extension Name
 * @author Your Name
 * @link https://github.com/username
 *
 * @property Loader $load
 * @property Config $config
 * @property Cart\User $user
 * @property ModelSettingSetting $model_setting_setting
 * @property ModelExtensionModuleExample $model_extension_module_example
 */
class ControllerExtensionModuleExample extends Controller
{
```

Blank line after `<?php`. Credits header is name, `@author`, `@link` — **no `@version`**; the version lives in `install.xml` and a `VERSION` constant. Declare every magic dependency as `@property`. Class name mirrors the file path; model properties are `model_` + path with underscores.

---

## 4. Methods and events
Everything is typed — properties, parameters and returns.

```php
private array $error = [];

public function index(): void {}
protected function validate(): bool {}
public function getOrders(array $data = []): array {}
private function getCost(array $products, array $profile): float {}
```

A closure whose body is one expression is `fn`. It captures what it reads by itself, so it has no `use`, and its expression can be an array literal across several lines. A closure that needs statements is `function`.

```php
$data['histories'] = array_map(fn (array $history): array => [
    'comment'    => $history['notify'] ? nl2br($history['comment']) : '',
    'date_added' => date($date_format, strtotime($history['date_added'])),
], $histories);
```

Event method names mirror the trigger in camelCase — `admin/view/sale/order_info/after` → `viewSaleOrderInfoAfter`. Core hands each hook its own arguments:

```php
public function controllerCommonHeaderBefore(string $route = '', array &$data = []): void {}
public function controllerCommonHeaderAfter(string $route = '', array $data = [], ?string &$output = null): void {}
public function viewCommonColumnLeftBefore(string $route = '', array &$data = []): void {}
public function viewSaleOrderInfoAfter(string $route = '', array $data = [], string &$output = ''): void {}
public function modelCatalogProductGetProductBefore(string $route = '', array &$args = []): void {}
public function modelExtensionPaymentGetMethodAfter(string $route = '', array $args = [], array &$output = []): void {}
public function modelCatalogProductAddProductAfter(string $route = '', array $args = [], int $product_id = 0): void {}
```

A view hook's second argument is the template data; a model hook's is the method's own arguments, in order. Take by reference only what the handler changes.

**Handlers return nothing.** Core stops at the first handler on a trigger that returns a non-null value: that value replaces the output, a `before` handler's value skips the call it guards, and every handler queued after it never runs. Change what you are handed through the references instead.

Triggers are stored with an `admin/` or `catalog/` prefix and take `*` wildcards: `catalog/model/extension/payment/*/getMethod/after`.

Prefer single-word action names — `brands`, `synced`, `refresh`, `push` — over `brandMapping`, `syncedProducts`, `refreshCatalog`. They are URL routes.

---

## 5. Install / uninstall
Core runs first. `extension/extension/{type}/install` records the extension and grants the current user group `access` and `modify` on `extension/{type}/{name}`, then calls `install()`. `install()` grants them as well, so the extension never depends on how it was installed. `addPermission()` appends without checking and leaves a duplicate entry behind; that is harmless, because `removePermission()` strips every copy with `array_diff()`.

Order is strict: permissions, database, events, settings. Phase markers are required here.

```php
public function install(): void
{
    // Permissions
    $this->load->model('user/user_group');

    $this->model_user_user_group->addPermission($this->user->getGroupId(), 'access', self::ROUTE);
    $this->model_user_user_group->addPermission($this->user->getGroupId(), 'modify', self::ROUTE);

    // Database
    $this->load->model(self::ROUTE);

    $this->model_extension_module_example->install();

    // Events
    $this->load->model('setting/event');

    $this->model_setting_event->deleteEventByCode(self::CODE);

    foreach (self::EVENTS as $trigger => $action) {
        $this->model_setting_event->addEvent(self::CODE, $trigger, $action);
    }

    // Settings
    $this->load->model('setting/setting');

    $this->model_setting_setting->editSetting(self::CODE, array_merge([
        self::CODE . '_status' => 0,
    ], $this->model_setting_setting->getSetting(self::CODE)));
}

public function uninstall(): void
{
    // Permissions
    $this->load->model('user/user_group');

    $this->model_user_user_group->removePermission($this->user->getGroupId(), 'access', self::ROUTE);
    $this->model_user_user_group->removePermission($this->user->getGroupId(), 'modify', self::ROUTE);

    // Events
    $this->load->model('setting/event');

    $this->model_setting_event->deleteEventByCode(self::CODE);
}
```

Core strips the permissions from every user group itself only from 3.0.4.0; before that it leaves them, so `uninstall()` removes the current group's. Never loop `removePermission()` over every group: it breaks on a group that has no permission list of that type.

`uninstall()` drops only the tables it can rebuild from nothing — caches, logs, short-lived tokens. A table holding what the store entered or what an outside service reported — redirects, a blacklist, notes, a payment gateway's transaction responses — stays, so an uninstall never costs the shop its data. An extension with no disposable table has no database phase in `uninstall()` at all.

`uninstall()` never calls `deleteSetting()`, because the core already has. On every 3.0.x, `extension/extension/{type}/uninstall` runs `model_setting_extension->uninstall()`, which deletes the extension row and every `{type}_{code}` setting before the extension's own `uninstall()` is called. Settings therefore never survive an uninstall: a reinstall resets them to the defaults written in `install()`, and nothing an extension does can change that. The kept tables are what carries over.

`editSetting()` deletes every row for the code before writing, and writes back only the keys that start with the code. Defaults are therefore merged **under** what is stored, never over it, and anything the array leaves out is gone after a save — keep runtime state such as a token or a last-run time in a table, never beside the form's fields.

Reinstalling is how a schema migration is applied: `install()` finds the kept table and brings it up to date. Model side: `CREATE TABLE IF NOT EXISTS` plus a `migrate()` that checks `SHOW COLUMNS … LIKE` before each `ALTER`.

---

## 6. Database
```php
$query = $this->db->query("
    SELECT * FROM `" . DB_PREFIX . "example_request`
    WHERE `order_id` = {$order_id}
      AND `status` = 1
    LIMIT 1
");

$this->db->query("
    UPDATE `" . DB_PREFIX . "example_request`
    SET `region` = '" . $this->db->escape($region) . "'
    WHERE `order_id` = {$order_id}
");
```

Backtick every table and column, prepend `DB_PREFIX`. **Integers go in bare** — `{$order_id}`, `= 1`, `IN (" . implode(', ', array_map('intval', $ids)) . ")`. An empty list makes `IN ()`, a syntax error, so return before the query when there is nothing to match. Quotes are for genuine strings, always through `db->escape()`. Results: `$query->row`, `$query->rows`, `$query->num_rows`; then `getLastId()`, `countAffected()`. No prepared statements — `escape()` is the mechanism.

**A `LEFT JOIN` is where a null comes from.** `DB` hands back every column as a string, so the only values that reach PHP as null are the ones a join missed — the status row of an order whose status is gone, the product row of a deleted product — plus the handful of columns that are nullable in their own right (`oc_product.image`). **Cast those at the point of use**, and leave the query alone: `COALESCE` in the select list buries the query under noise for something one `(string)` says better.

```php
$status_name = (string)$order['status_name'];   // os.name, LEFT JOIN, missing status
$image = $exists ? (string)$product['image'] : '';
```

Check `information_schema.COLUMNS` rather than guessing which columns are nullable; most of OpenCart's are `NOT NULL` with a default, so the join is usually the only risk.

---

## 7. Config
```php
$status = (int)$this->config->get(self::CODE . '_status');
$title = $this->config->get(self::CODE . '_title')[(int)$this->config->get('config_language_id')] ?? '';
```

Keys are always `{CODE}_{field}`. Type-cast on every read. Per-language values are arrays keyed by `language_id`.

Core reads `{CODE}_status` itself: it fills the Enabled column of the extension list, and for payment, shipping and total extensions it decides whether checkout calls the extension at all. Total extensions are also ordered by `{CODE}_sort_order`.

---

## 8. Language files
```php
$_ = [
    // Heading
    'heading_title'    => 'Extension Name',

    // Text
    'text_extension'   => 'Extensions',
    'text_success'     => 'Success: You have successfully modified Extension Name!',
    'text_edit'        => 'Edit Extension Name',

    // Entry
    'entry_status'     => 'Status',

    // Error
    'error_permission' => 'Warning: You do not have permission to modify Extension Name!',
];
```

One `$_` array, grouped by prefix with section markers, pairs aligned. Prefixes: `heading_`, `text_`, `tab_`, `entry_`, `column_`, `button_`, `help_`, `error_`.

Always include `heading_title`, `text_extension`, `text_success`, `text_edit`, `entry_status`, `error_permission`. `text_edit` is the panel heading, `heading_title` the page title.

**Never redefine a key core already gives every page.** Admin's `en-gb.php` has `text_yes`, `text_no`, `text_none`, `text_select`, `text_enabled`, `text_disabled`, `text_default`, `text_all_zones`, `text_loading`, `text_no_results`, `text_confirm`, `text_home`, `text_pagination`, and the `button_` set — `button_save`, `button_cancel`, `button_add`, `button_edit`, `button_delete`, `button_remove`, `button_filter` and more. The catalog's `en-gb.php` has a smaller set of its own.

**Read `admin/language/en-gb/en-gb.php` before adding any key**, date and number formats especially: `date_format_short` (`d/m/Y`), `date_format_long`, `time_format`, `datetime_format` (`d/m/Y H:i:s`), `decimal_point`, `thousand_point`. A format key of your own duplicates one of these and ignores a store that has customised its own. Use core's formats exactly as they are, and never assemble a format from them: a date with a time is `datetime_format`, seconds included, and never `date_format_short . ' H:i'`.

**Never assign a language key into `$data`.** A plain `load->language()` makes every key available to the template already, in admin and catalog alike:

```php
$this->load->language(self::ROUTE);
```
```twig
{{ heading_title }}
{{ entry_status }}
```

Writing `$data['heading_title'] = $this->language->get('heading_title');` is noise that grows by a line per string. Use `$this->language->get()` in PHP only for strings going somewhere other than the template — a `$json` response, an exception, `sprintf()`.

**Code core runs inside another page must namespace** — an event handler, a payment `order()` tab, a dashboard widget. They share the host page's language pool. An event handler's keys overwrite the host's own outright. A controller core loads into the page gets the host's keys put back once it returns, but every new key it loaded stays behind and reaches the host's template. On 3.0.2.0 the keys are not put back: a duplicate `controller/*/after` key in `system/config/catalog.php` overrides core's restore event, so a page that loads columns or modules reloads its own language after them. Pass `self::CODE` as the second argument:

```php
$this->load->language(self::ROUTE, self::CODE);

$heading = $this->language->get(self::CODE)->get('heading_title');
```

The host template cannot reach your namespace, so a handler hands its strings over itself:

```php
foreach ($this->language->get(self::CODE)->data ?? [] as $key => $value) {
    $data[$key] = $value;
}
```

`$this->language->data ?? []` is the same loop against the shared pool, for a handler that loaded without a namespace. Prefer the namespaced form; the `?? []` degrades instead of fataling on someone else's page.

---

## 9. Input, output and URLs
**Request input arrives escaped.** `Request` runs every GET, POST, COOKIE, FILES and SERVER value through `trim(htmlspecialchars($value, ENT_COMPAT, 'UTF-8'))` as it is built, so `$this->request->post['name']` holds `Tom &amp; Jerry`, without its surrounding spaces. That is the form OpenCart stores and its templates print, so save it as it arrives. Decode it with `html_entity_decode($value, ENT_QUOTES, 'UTF-8')` the moment it leaves for anywhere else — an API body, a hash or signature check, an email subject, a CSV or XML file. Settings saved from a form are request input too: a secret containing `&` reads back from config as `&amp;`.

**Twig prints raw.** `autoescape` is off on every 3.0.x. A value that came through the request or out of OpenCart's tables is escaped already and prints as it is. Anything else — an API response, a file, a string you decoded — prints through `|e`, exactly once.

**URLs** always take the array form, always HTTPS:

```php
$this->url->link(self::ROUTE, ['user_token' => $this->session->data['user_token']], true);
```

Never `'user_token=' . $token`. `link()` returns its query joined with `&amp;`, ready for an HTML attribute, and `$this->response->redirect()` decodes it itself. Everywhere else — a JSON value, a URL handed to JavaScript, an email — decode it first with `str_replace('&amp;', '&', $url)`. `|e('js')` keeps the `&amp;`, and the request then arrives without its `user_token`.

Every controller with an AJAX action carries the same helper, named `json`:

```php
private function json(array $data): void
{
    $this->response->addHeader('Content-Type: application/json');
    $this->response->setOutput(json_encode($data));
}
```

---

## 10. ocmod XML
```xml
<?xml version="1.0" encoding="UTF-8"?>
<modification>
    <name>Extension Name</name>
    <code>module_example</code>
    <version>1.0</version>
    <author>Your Name</author>
    <link>https://github.com/username</link>

    <file path="catalog/controller/product/{category,product}.php">
        <operation error="skip">
            <search><![CDATA[$data['breadcrumbs'] = array();]]></search>
            <add position="before"><![CDATA[injected code]]></add>
        </operation>
    </file>
</modification>
```

`<code>` matches `self::CODE`. Nothing applies until Extensions › Modifications › Refresh, which writes every result to the modification log. All content in CDATA.

ocmod reaches PHP files and Twig templates, never JavaScript or CSS: the browser fetches those straight from disk, past the modification cache. To change a core script, ship a file of the extension's own and load it beside it.

| Attribute | On | Meaning |
|---|---|---|
| `path` | `<file>` | A glob, braces included: `{category,product}` |
| `error` | `<operation>` | When the search finds nothing: `skip` skips this operation, `abort` drops the whole modification, anything else — `log` included — stops the remaining operations on that file |
| `trim` | `<search>` | On unless `trim="false"` |
| `index` | `<search>` | Comma-separated, 0-based list of matches to change; every match when absent |
| `regex` | `<search>` | `true` makes the search a preg pattern over the whole file; `limit` caps the matches |
| `position` | `<add>` | `replace` (the default), `before`, `after` |
| `offset` | `<add>` | Moves the insertion that many lines away from the match |
| `trim` | `<add>` | Off unless `trim="true"` |
| `<ignoreif>` | `<operation>` | Skips the operation when this text (or `regex="true"` pattern) is already in the file |

Use `skip` when each operation stands alone, `abort` when a half-applied set would break the file.

**ocmod matches per line.** A multi-line `<search>` never matches and shows as NOT FOUND in the log. Keep the search to one line, or use `regex`, and verify against the compiled file in `system/storage/modification/`.

---

## 11. Code style
- 4 spaces PHP, 2 spaces Twig and JS. Max 120 chars PHP.
- **PHP 7.4 is the floor.** Every property is typed (`private array $error = [];`), `$x ??= $y` replaces `$x = $x ?? $y`, and a closure whose body is one expression is `fn` (§4). Nothing from PHP 8: no `match`, nullsafe `?->`, named arguments, union or `mixed` types, constructor promotion, `str_contains()`, `str_starts_with()`, `str_ends_with()`, `throw` as an expression, or a trailing comma in a parameter list.
- **Every `input` and `textarea` carries a `placeholder`.** Where there is nothing better to say, repeat the field's own label: `placeholder="{{ entry_title }}"`. Applies to readonly fields too, so a field that fails to populate still says what it is rather than sitting blank.
- Short array syntax. `??` and `??=` over `isset`. `static fn` or `static function` when `$this` is unused.
- No `goto`. No single-line control flow — every body is braced on its own lines.
- UTF-8, LF.
- **No `use` imports in a namespaced file.** Fully qualify at point of use: `\Registry $registry`, `/** @var \DB */`, `new \XMLWriter()`, and return types — `: \XMLWriter`. Own-namespace classes stay bare.
- **Alignment:** only `key => value` inside an array literal. Constants, assignments, doc tags, argument defaults, SQL columns — one space, even when ten lines would line up. Aligned statements produce a whole-block diff when one name grows a character; an array reads as a table and does not.
- **Naming:** variables and properties snake_case, methods camelCase, classes PascalCase, constants UPPER_SNAKE.
- **No abbreviations**, in any name — variables, methods, config keys, language keys, columns, CSS classes, Twig variables. `$language_id` not `$lang_id`; also `$description`, `$category`, `$message`, `$timestamp`, `$quantity`, `$image`, `$button`, `$separator`. Acceptable only where the short form is no longer an abbreviation — `$id`, `$url`, `$db`, `$sql`, `$xml`, `$api` — or where OpenCart's schema forces it, in which case match the framework exactly.
- **Loader, never `require_once`:** `$this->load->library('name')`, `$this->load->model(self::ROUTE)`.

**Cast where it says something.** `DB` returns every column as a string, so `(string)$order['email']` states the obvious, and `(string)($value ?? '')` states it twice — `??` has already yielded the string. `preg_replace()` and `preg_replace_callback()` over a string subject hand back a string too, so leave them bare — unless the pattern carries `/u` and the subject may be broken UTF-8, where they answer null. A cast earns its place where the type genuinely changes or the value genuinely varies:

| Cast | Why it stays |
|---|---|
| `(int)`, `(float)`, `(bool)` | A string column is not a number or a flag. Cast at the boundary, once. |
| `$this->config->get(…)` | Returns whatever was stored, an array included. |
| `$this->request->get` / `post` | `?code[]=1` arrives as an array. |
| a `foreach` key | PHP turns a numeric-looking key into an `int`. |
| `strpos()`, `strstr()`, `array_search()`, `json_decode()` | They answer `false` or `null`. |

And the reason it matters: a userland parameter typed `string` does **not** coerce `null`, even in coercive mode — it throws a `TypeError`. So a cast over a value a `LEFT JOIN` can miss really is holding the code up (§6), while the same cast over a `NOT NULL` column is noise.

**Loader calls stack by type only.** Consecutive `load->language()`, `load->library()`, `load->model()` or `document->` calls form a block of one kind; a blank line closes every block, whether the next line is another kind of load or the code that uses it:

```php
$this->load->language(self::ROUTE);

$this->load->library('example');

$this->load->model('setting/setting');
$this->load->model(self::ROUTE);

$this->model_extension_module_example->install();
```

Never mix kinds in one block, and never call a model on the line straight after loading it — even a single load followed by a single call gets the blank line.

---

## 12. Settings page
Never redirect after save; redisplay with `$data['success']` inline. Phase markers are required.

```php
public function index(): void
{
    $this->load->language(self::ROUTE);

    $this->document->setTitle($this->language->get('heading_title'));

    $data = [];

    // Save
    if ($this->request->server['REQUEST_METHOD'] === 'POST' && $this->validate()) {
        $this->load->model('setting/setting');

        $this->model_setting_setting->editSetting(self::CODE, $this->request->post);

        $data['success'] = $this->language->get('text_success');
    }

    // Errors
    $data['errors'] = [];

    foreach (self::ERRORS as $error) {
        $data['errors'][$error] = $this->error[$error] ?? '';
    }

    // Breadcrumbs
    $user_token = $this->session->data['user_token'];
    $cancel = $this->url->link('marketplace/extension', ['user_token' => $user_token, 'type' => 'module'], true);
    $action = $this->url->link(self::ROUTE, ['user_token' => $user_token], true);

    $data['breadcrumbs'] = [
        [
            'text' => $this->language->get('text_home'),
            'href' => $this->url->link('common/dashboard', ['user_token' => $user_token], true),
        ],
        [
            'text' => $this->language->get('text_extension'),
            'href' => $cancel,
        ],
        [
            'text' => $this->language->get('heading_title'),
            'href' => $action,
        ],
    ];

    $data['action'] = $action;
    $data['cancel'] = $cancel;

    // Fields
    foreach (self::FIELDS as $field) {
        $key = self::CODE . '_' . $field;
        $data[$field] = $this->request->post[$key] ?? $this->config->get($key);
    }

    // Layout
    $data['header'] = $this->load->controller('common/header');
    $data['column_left'] = $this->load->controller('common/column_left');
    $data['footer'] = $this->load->controller('common/footer');

    $this->response->setOutput($this->load->view(self::ROUTE, $data));
}
```

Fields read the post before config because config was loaded before the save and still holds the old values.

The template reads `{{ errors.warning }}`, never `error_warning`, and the panel title is `{{ text_edit }}`. The tab strip sits directly in `panel-body`, and the save button reaches the form through `form="form-{type}"`. Bootstrap 3 switches a pane only among its siblings, so every pane keeps one parent: settings in a single tab put the `<form>` inside `#tab-general`; settings spread over several tabs put it around the whole `tab-content`. Tool tabs never hold a `<form>` — forms cannot nest — and post through `fetch()`.

**About tab.** Every settings page ends with one, always the last tab. The credits are the ones from the header docblock, written straight into the template — a name and a URL are not translatable, so they never become language keys:

```twig
<li><a href="#tab-about" data-toggle="tab">{{ tab_about }}</a></li>
…
<div class="tab-pane" id="tab-about">
  <fieldset>
    <legend>{{ text_developer_info }}</legend>
    <dl class="dl-horizontal">
      <dt>{{ text_developer }}</dt>
      <dd>Your Name</dd>
      <dt>{{ text_website }}</dt>
      <dd><a href="https://website.com" target="_blank">website.com</a></dd>
      <dt>{{ text_github }}</dt>
      <dd><a href="https://github.com/username" target="_blank">@username</a></dd>
      <dt>{{ text_email }}</dt>
      <dd><a href="mailto:you@website.com">you@website.com</a></dd>
    </dl>
  </fieldset>
  <fieldset>
    <legend>{{ text_support }}</legend>
    <p>{{ text_support_body }}</p>
  </fieldset>
</div>
```

Drop a row the credits have no value for, and its key with it; never leave one blank. The keys and their wording are fixed:

| Key | en-gb | el-gr |
|---|---|---|
| `tab_about` | About | Σχετικά |
| `text_developer_info` | Developer Information | Πληροφορίες Προγραμματιστή |
| `text_developer` | Developer | Προγραμματιστής |
| `text_website` | Website | Ιστοσελίδα |
| `text_github` | GitHub | GitHub |
| `text_email` | Email | Email |
| `text_support` | Support | Υποστήριξη |
| `text_support_body` | For support, bug reports, or feature requests, please contact the developer. | Για υποστήριξη, αναφορά σφαλμάτων ή αιτήματα λειτουργιών, επικοινωνήστε με τον προγραμματιστή. |

---

## 13. Comments
Write almost none. The reader knows PHP and does not need it narrated.

A comment is justified only where the mechanism is opaque — a framework or browser quirk, an ordering constraint that looks arbitrary, a spec rule. Expect zero to two per file. When in doubt, delete it.

```php
// redirect() exits before output(), so a page cache would store an empty body.
header('Location: ' . $url);
```

Never write: a docblock describing what a method does; a line restating the next line; an explanation of code that is not there; a block label; a past-tense verb (added, removed, changed, fixed); "this now handles"; an end-of-line comment. Never comment out code — delete it, git has it. Rationale belongs in the commit message.

Not comments, and they stay: `@property`, `@var`, `@param`, `@return`; the credits header; language-file section markers; phase markers in `install()`, `uninstall()` and a settings `index()`. A marker names a phase of a fixed sequence — it never explains what the code under it does. Comments in this document annotate the document; do not copy them.

Twig and SQL follow the same rule.

---

## 14. JavaScript
ES6. `const` and `let`, never `var`. No jQuery for new code — it is loaded, so use `$` only for Bootstrap's `.modal()` and `.tab()`. Never wrap a script in `<!-- //-->`.

Take exactly one global, named after the extension; the admin page is shared ground.

```js
const seo = {
  post: async (url, params) => {
    const response = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded', 'X-Requested-With': 'XMLHttpRequest'},
      body: params instanceof URLSearchParams ? params : new URLSearchParams(params || {})
    });

    return response.json();
  }
};
```

`X-Requested-With` is required. Core never reads it, but our own libraries do: the Nitro page cache and the Atlas SEO and Ultimate SEO redirects step aside for any request carrying it, so without it an AJAX call can come back cached or redirected.

URLs reach a script still carrying `&amp;` (§9). Hand one over in a `data-` attribute exactly as `link()` returned it, and the browser decodes it; decode it in PHP only when it goes straight into a script or a JSON value.

**Every async call is awaited, returned, or `void`.** A bare call reads as a forgotten `await`:

```js
await reload();
reload: () => load(build(true)),
button.addEventListener('click', () => void load(url));
```

**Delegate handlers** for anything inside AJAX-loaded content, binding on a container that survives the swap. **Escape every Twig value** crossing into JS with `|e('js')`.

Two Bootstrap 3 traps: `shown.bs.tab` is a jQuery event a native listener never sees, so hang tab loading off the anchor's `click`; tooltips are wired once on ready, so fetched content has none until re-initialised, and inside a scrolling container they need `container: 'body'`.

Section 13 applies here without exception.
