# Payment extensions

Read with SKILL.md; everything there applies here too.

## Checkout contract

The extension's name is its code everywhere: the file names, `oc_extension.code`, `payment_{name}_status`, and the `code` that `getMethod()` returns. That code is what lands in `oc_order.payment_code`.

- **`getMethod(array $address, float $total): array`** in the catalog model. Checkout calls it for every installed payment whose `payment_{name}_status` is set, and offers the method when it returns `['code' => '{name}', 'title' => …, 'terms' => '', 'sort_order' => …]`; an empty array hides it. Methods are ordered by that `sort_order`.
- **`index(): string`** in the catalog controller. The confirm step renders its output under the order summary — the confirm button, or the form that posts to the gateway. It returns the view, never sets output.
- **The order already exists.** Confirm writes it with `order_status_id` 0, which admin lists under Missing Orders, before the customer pays. It becomes a real order only when the extension calls `addOrderHistory()` with a status; moving it into a processing or complete status is what subtracts stock.

## Order page tab

`sale/order/info` gives the order's payment extension a tab beside History and Additional. Core calls the **admin** route `extension/payment/{name}/order` and puts the returned string in the tab; an empty string means no tab. The tab title is your `heading_title`, which core reads from your language file itself.

Core makes the call only when the user has `access` on `extension/payment/{name}` **and** `catalog/controller/extension/payment/{name}.php` exists: the catalog file, although `order()` lives in the admin controller.

```php
public function order(): string
{
    $this->load->model('setting/extension');

    // Before 3.0.4.0 uninstalling leaves the access permission in place, and the table is gone.
    if (!in_array('example', $this->model_setting_extension->getInstalled('payment'), true)) {
        return '';
    }

    $this->load->language(self::ROUTE, self::CODE);

    $this->load->model(self::ROUTE);

    $data = [];

    foreach ($this->language->get(self::CODE)->data ?? [] as $key => $value) {
        $data[$key] = $value;
    }

    $date_format = $this->language->get('datetime_format');
    $order_id = (int)($this->request->get['order_id'] ?? 0);

    $data['transactions'] = [];

    foreach ($this->model_extension_payment_example->getTransactions($order_id) as $transaction) {
        $data['transactions'][] = [
            'date_added' => date($date_format, strtotime($transaction['date_added'])),
            'amount'     => $this->currency->format((float)$transaction['amount'], $transaction['currency'], 1.0),
            'status'     => $transaction['status'],
        ];
    }

    return $this->load->view(self::ROUTE . '_order', $data);
}
```

- **Namespace the language** (§8). `order()` is a controller core loads into the order page, so every new key it loads would reach the order page's template.
- **Guard on installed, not on permission.** Core checks the permission, but before 3.0.4.0 an uninstall leaves it on every user group but the one that uninstalled. When `order()` reads a table `uninstall()` drops, check `getInstalled('payment')` first, or the order page dies on the missing table for everyone else.
- **An empty string hides the tab.** Return `''` only when there is nothing to show. When "nothing recorded" is itself the information, return a line saying so.
- **Amounts stored in the charged currency take a value of `1.0`.** Leave the value out and `Cart\Currency` multiplies by the currency's rate a second time. Check `$this->currency->has()` first: the currency may since have been deleted, and `format()` does not check.
- **The template is a fragment**: `view/template/extension/payment/{name}_order.twig`, with no header or footer, holding a `table-responsive` table or a single `<p>`.
- **Nothing to install.** No event, setting or permission is involved, so a store gets the tab by uploading the files.
