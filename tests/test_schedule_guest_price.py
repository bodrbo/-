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


if __name__ == "__main__":
    unittest.main()
