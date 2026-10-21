import unittest

from support import application_module


class ScheduleGuestPriceTests(unittest.TestCase):
    """A group programme is priced per guest, so the price of a booking is
    that price times the guests. The form does the arithmetic in JavaScript,
    so these tests pin the pieces of the page that make it happen."""

    def setUp(self):
        application_module.init_db()
        application_module.app.config.update(TESTING=True)
        self.client = application_module.app.test_client()
        with self.client.session_transaction() as session:
            session["admin_id"] = 1
            session["admin_name"] = "Администратор"
        self.page = self.client.get("/schedule").get_data(as_text=True)

    def test_the_guests_field_of_a_booking_recalculates_the_price(self):
        self.assertIn(
            'id="scheduleBookingGuestsCount" min="1" max="1000" placeholder="Необязательно"\n'
            '                 oninput="syncScheduleClientPrices()"', self.page)

    def test_a_group_booking_is_priced_per_guest(self):
        start = self.page.index("function scheduleServicePriceForBooking")
        body = self.page[start:self.page.index("function syncScheduleClientPrices", start)]
        self.assertIn("scheduleBookingGuestsCount", body)
        self.assertIn("Number(selected.dataset.price || 0) * (", body)

    def test_a_saved_price_that_follows_the_formula_keeps_following_the_guests(self):
        start = self.page.index("function addScheduleParticipant")
        body = self.page[start:self.page.index("function removeScheduleParticipant", start)]
        self.assertIn("perGuest * Math.max(1, Number(guestsCount || 1))", body)
        self.assertIn("priceInput.dataset.priceMode = 'auto'", body)
        self.assertIn("startsWith('orders:')", body)       # imported prices stay as they are

    def test_group_event_participants_are_priced_per_guest(self):
        start = self.page.index("function syncScheduleClientPrices")
        body = self.page[start:self.page.index("function syncScheduleRevenueTotal", start)]
        self.assertIn("pricePerGuest * Math.max(0, guests)", body)

    def test_the_price_a_manager_types_is_per_guest_and_the_total_is_what_is_sent(self):
        # participants: a visible per-guest field (not submitted) and a hidden total (submitted)
        self.assertIn('<span>Цена за гостя</span>', self.page)
        self.assertIn('data-guest-price value="0"', self.page)
        self.assertIn('<input type="hidden" name="participant_price[]" value="0" data-price-mode="auto">', self.page)
        self.assertNotIn('name="guest_price', self.page.split("function buildScheduleDetailClientEditForm")[0])
        # booking: the same pair, with a label that follows the kind of service
        self.assertIn('id="scheduleCustomerGuestPrice" data-guest-price', self.page)
        self.assertIn('<input type="hidden" name="customer_price" id="scheduleCustomerPrice" value="0" data-price-mode="auto">', self.page)
        self.assertIn("'Цена за гостя, ₽' : 'Стоимость, ₽'", self.page)

    def test_a_typed_price_is_multiplied_by_the_guests_and_follows_them(self):
        start = self.page.index("function scheduleRefreshPrice")
        body = self.page[start:self.page.index("function scheduleGuestPriceTyped", start)]
        self.assertIn("scheduleRoundMoney(exact * ctx.guests)", body)
        typed = self.page[self.page.index("function scheduleGuestPriceTyped"):self.page.index("function scheduleRefreshAllPrices")]
        self.assertIn("typed * ctx.guests", typed)
        self.assertIn("priceMode = 'manual'", typed)

    def test_an_imported_total_is_not_rescaled_by_the_guests(self):
        body = self.page[self.page.index("function schedulePriceContext"):self.page.index("function scheduleRoundMoney")]
        self.assertGreaterEqual(body.count("startsWith('orders:')"), 2)

    def test_the_edit_form_of_a_guest_in_the_detail_view_takes_a_per_guest_price(self):
        body = self.page[self.page.index("function buildScheduleDetailClientEditForm"):self.page.index("function scheduleApplyParticipantUpdate")]
        self.assertIn("<span>Цена за гостя, ₽</span>", body)
        self.assertIn("price: String(editedTotal())", body)
        self.assertIn("perGuest * guests", body)


if __name__ == "__main__":
    unittest.main()
