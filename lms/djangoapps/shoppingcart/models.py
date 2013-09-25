from datetime import datetime
import pytz
import logging
import smtplib
import textwrap

from django.db import models
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.mail import send_mail
from django.contrib.auth.models import User
from django.utils.translation import ugettext as _
from django.db import transaction
from model_utils.managers import InheritanceManager

from course_modes.models import CourseMode
from courseware.courses import get_course_about_section
from mitxmako.shortcuts import render_to_string
from student.views import course_from_id
from student.models import CourseEnrollment
from dogapi import dog_stats_api
from verify_student.models import SoftwareSecurePhotoVerification
from xmodule.modulestore.django import modulestore
from xmodule.course_module import CourseDescriptor

from .exceptions import InvalidCartItem, PurchasedCallbackException

log = logging.getLogger("shoppingcart")

ORDER_STATUSES = (
    ('cart', 'cart'),
    ('purchased', 'purchased'),
    ('refunded', 'refunded'),  # Not used for now
)


class Order(models.Model):
    """
    This is the model for an order.  Before purchase, an Order and its related OrderItems are used
    as the shopping cart.
    FOR ANY USER, THERE SHOULD ONLY EVER BE ZERO OR ONE ORDER WITH STATUS='cart'.
    """
    user = models.ForeignKey(User, db_index=True)
    currency = models.CharField(default="usd", max_length=8)  # lower case ISO currency codes
    status = models.CharField(max_length=32, default='cart', choices=ORDER_STATUSES)
    purchase_time = models.DateTimeField(null=True, blank=True)
    # Now we store data needed to generate a reasonable receipt
    # These fields only make sense after the purchase
    bill_to_first = models.CharField(max_length=64, blank=True)
    bill_to_last = models.CharField(max_length=64, blank=True)
    bill_to_street1 = models.CharField(max_length=128, blank=True)
    bill_to_street2 = models.CharField(max_length=128, blank=True)
    bill_to_city = models.CharField(max_length=64, blank=True)
    bill_to_state = models.CharField(max_length=8, blank=True)
    bill_to_postalcode = models.CharField(max_length=16, blank=True)
    bill_to_country = models.CharField(max_length=64, blank=True)
    bill_to_ccnum = models.CharField(max_length=8, blank=True)  # last 4 digits
    bill_to_cardtype = models.CharField(max_length=32, blank=True)
    # a JSON dump of the CC processor response, for completeness
    processor_reply_dump = models.TextField(blank=True)

    @classmethod
    def get_cart_for_user(cls, user):
        """
        Always use this to preserve the property that at most 1 order per user has status = 'cart'
        """
        # find the newest element in the db
        try:
            cart_order = cls.objects.filter(user=user, status='cart').order_by('-id')[:1].get()
        except ObjectDoesNotExist:
            # if nothing exists in the database, create a new cart
            cart_order, _created = cls.objects.get_or_create(user=user, status='cart')
        return cart_order

    @property
    def total_cost(self):
        """
        Return the total cost of the cart.  If the order has been purchased, returns total of
        all purchased and not refunded items.
        """
        return sum(i.line_cost for i in self.orderitem_set.filter(status=self.status))

    def clear(self):
        """
        Clear out all the items in the cart
        """
        self.orderitem_set.all().delete()

    def purchase(self, first='', last='', street1='', street2='', city='', state='', postalcode='',
                 country='', ccnum='', cardtype='', processor_reply_dump=''):
        """
        Call to mark this order as purchased.  Iterates through its OrderItems and calls
        their purchased_callback

        `first` - first name of person billed (e.g. John)
        `last` - last name of person billed (e.g. Smith)
        `street1` - first line of a street address of the billing address (e.g. 11 Cambridge Center)
        `street2` - second line of a street address of the billing address (e.g. Suite 101)
        `city` - city of the billing address (e.g. Cambridge)
        `state` - code of the state, province, or territory of the billing address (e.g. MA)
        `postalcode` - postal code of the billing address (e.g. 02142)
        `country` - country code of the billing address (e.g. US)
        `ccnum` - last 4 digits of the credit card number of the credit card billed (e.g. 1111)
        `cardtype` - 3-digit code representing the card type used (e.g. 001)
        `processor_reply_dump` - all the parameters returned by the processor

        """
        if self.status == 'purchased':
            return
        self.status = 'purchased'
        self.purchase_time = datetime.now(pytz.utc)
        self.bill_to_first = first
        self.bill_to_last = last
        self.bill_to_city = city
        self.bill_to_state = state
        self.bill_to_country = country
        self.bill_to_postalcode = postalcode
        if settings.MITX_FEATURES['STORE_BILLING_INFO']:
            self.bill_to_street1 = street1
            self.bill_to_street2 = street2
            self.bill_to_ccnum = ccnum
            self.bill_to_cardtype = cardtype
            self.processor_reply_dump = processor_reply_dump

        # save these changes on the order, then we can tell when we are in an
        # inconsistent state
        self.save()
        # this should return all of the objects with the correct types of the
        # subclasses
        orderitems = OrderItem.objects.filter(order=self).select_subclasses()
        for item in orderitems:
            item.purchase_item()

        # send confirmation e-mail
        subject = _("Order Payment Confirmation")
        message = render_to_string('emails/order_confirmation_email.txt', {
            'order': self,
            'order_items': orderitems
        })
        try:
            send_mail(subject, message,
                      settings.DEFAULT_FROM_EMAIL, [self.user.email])
        except smtplib.SMTPException:
            log.error('Failed sending confirmation e-mail for order %d', self.id)


class OrderItem(models.Model):
    """
    This is the basic interface for order items.
    Order items are line items that fill up the shopping carts and orders.

    Each implementation of OrderItem should provide its own purchased_callback as
    a method.
    """
    objects = InheritanceManager()
    order = models.ForeignKey(Order, db_index=True)
    # this is denormalized, but convenient for SQL queries for reports, etc. user should always be = order.user
    user = models.ForeignKey(User, db_index=True)
    # this is denormalized, but convenient for SQL queries for reports, etc. status should always be = order.status
    status = models.CharField(max_length=32, default='cart', choices=ORDER_STATUSES)
    qty = models.IntegerField(default=1)
    unit_cost = models.DecimalField(default=0.0, decimal_places=2, max_digits=30)
    line_desc = models.CharField(default="Misc. Item", max_length=1024)
    currency = models.CharField(default="usd", max_length=8)  # lower case ISO currency codes
    fulfilled_time = models.DateTimeField(null=True)

    @property
    def line_cost(self):
        """ Return the total cost of this OrderItem """
        return self.qty * self.unit_cost

    @classmethod
    def add_to_order(cls, order, *args, **kwargs):
        """
        A suggested convenience function for subclasses.

        NOTE: This does not add anything to the cart. That is left up to the
        subclasses to implement for themselves
        """
        # this is a validation step to verify that the currency of the item we
        # are adding is the same as the currency of the order we are adding it
        # to
        currency = kwargs.get('currency', 'usd')
        if order.currency != currency and order.orderitem_set.exists():
            raise InvalidCartItem(_("Trying to add a different currency into the cart"))

    @transaction.commit_on_success
    def purchase_item(self):
        """
        This is basically a wrapper around purchased_callback that handles
        modifying the OrderItem itself
        """
        self.purchased_callback()
        self.status = 'purchased'
        self.fulfilled_time = datetime.now(pytz.utc)
        self.save()

    def purchased_callback(self):
        """
        This is called on each inventory item in the shopping cart when the
        purchase goes through.
        """
        raise NotImplementedError

    @property
    def single_item_receipt_template(self):
        """
        The template that should be used when there's only one item in the order
        """
        return 'shoppingcart/receipt.html'

    @property
    def single_item_receipt_context(self):
        """
        Extra variables needed to render the template specified in
        `single_item_receipt_template`
        """
        return {}

    @property
    def additional_instruction_text(self):
        """
        Individual instructions for this order item.

        Currently, only used for e-mails.
        """
        return ''


class PaidCourseRegistration(OrderItem):
    """
    This is an inventory item for paying for a course registration
    """
    course_id = models.CharField(max_length=128, db_index=True)
    mode = models.SlugField(default=CourseMode.DEFAULT_MODE_SLUG)

    @classmethod
    def part_of_order(cls, order, course_id):
        """
        Is the course defined by course_id in the order?
        """
        return course_id in [item.paidcourseregistration.course_id
                             for item in order.orderitem_set.all().select_subclasses("paidcourseregistration")]

    @classmethod
    @transaction.commit_on_success
    def add_to_order(cls, order, course_id, mode_slug=CourseMode.DEFAULT_MODE_SLUG, cost=None, currency=None):
        """
        A standardized way to create these objects, with sensible defaults filled in.
        Will update the cost if called on an order that already carries the course.

        Returns the order item
        """
        # TODO: Possibly add checking for whether student is already enrolled in course
        course = course_from_id(course_id)  # actually fetch the course to make sure it exists, use this to
                                            # throw errors if it doesn't

        ### handle default arguments for mode_slug, cost, currency
        course_mode = CourseMode.mode_for_course(course_id, mode_slug)
        if not course_mode:
            # user could have specified a mode that's not set, in that case return the DEFAULT_MODE
            course_mode = CourseMode.DEFAULT_MODE
        if not cost:
            cost = course_mode.min_price
        if not currency:
            currency = course_mode.currency

        super(PaidCourseRegistration, cls).add_to_order(order, course_id, cost, currency=currency)

        item, created = cls.objects.get_or_create(order=order, user=order.user, course_id=course_id)
        item.status = order.status

        item.mode = course_mode.slug
        item.qty = 1
        item.unit_cost = cost
        item.line_desc = 'Registration for Course: {0}. Mode: {1}'.format(get_course_about_section(course, "title"),
                                                                          course_mode.name)
        item.currency = currency
        order.currency = currency
        order.save()
        item.save()
        return item

    def purchased_callback(self):
        """
        When purchased, this should enroll the user in the course.  We are assuming that
        course settings for enrollment date are configured such that only if the (user.email, course_id) pair is found
        in CourseEnrollmentAllowed will the user be allowed to enroll.  Otherwise requiring payment
        would in fact be quite silly since there's a clear back door.
        """
        try:
            course_loc = CourseDescriptor.id_to_location(self.course_id)
            course_exists = modulestore().has_item(self.course_id, course_loc)
        except ValueError:
            raise PurchasedCallbackException(
                "The customer purchased Course {0}, but that course doesn't exist!".format(self.course_id))

        if not course_exists:
            raise PurchasedCallbackException(
                "The customer purchased Course {0}, but that course doesn't exist!".format(self.course_id))

        CourseEnrollment.enroll(user=self.user, course_id=self.course_id, mode=self.mode)

        log.info("Enrolled {0} in paid course {1}, paid ${2}".format(self.user.email, self.course_id, self.line_cost))
        org, course_num, run = self.course_id.split("/")
        dog_stats_api.increment(
            "shoppingcart.PaidCourseRegistration.purchased_callback.enrollment",
            tags=["org:{0}".format(org),
                  "course:{0}".format(course_num),
                  "run:{0}".format(run)]
        )


class CertificateItem(OrderItem):
    """
    This is an inventory item for purchasing certificates
    """
    course_id = models.CharField(max_length=128, db_index=True)
    course_enrollment = models.ForeignKey(CourseEnrollment)
    mode = models.SlugField()

    @classmethod
    @transaction.commit_on_success
    def add_to_order(cls, order, course_id, cost, mode, currency='usd'):
        """
        Add a CertificateItem to an order

        Returns the CertificateItem object after saving

        `order` - an order that this item should be added to, generally the cart order
        `course_id` - the course that we would like to purchase as a CertificateItem
        `cost` - the amount the user will be paying for this CertificateItem
        `mode` - the course mode that this certificate is going to be issued for

        This item also creates a new enrollment if none exists for this user and this course.

        Example Usage:
            cart = Order.get_cart_for_user(user)
            CertificateItem.add_to_order(cart, 'edX/Test101/2013_Fall', 30, 'verified')

        """
        super(CertificateItem, cls).add_to_order(order, course_id, cost, currency=currency)
        try:
            course_enrollment = CourseEnrollment.objects.get(user=order.user, course_id=course_id)
        except ObjectDoesNotExist:
            course_enrollment = CourseEnrollment.create_enrollment(order.user, course_id, mode=mode)

        # do some validation on the enrollment mode
        valid_modes = CourseMode.modes_for_course_dict(course_id)
        if mode in valid_modes:
            mode_info = valid_modes[mode]
        else:
            raise InvalidCartItem(_("Mode {mode} does not exist for {course_id}").format(mode=mode, course_id=course_id))
        item, _created = cls.objects.get_or_create(
            order=order,
            user=order.user,
            course_id=course_id,
            course_enrollment=course_enrollment,
            mode=mode
        )
        item.status = order.status
        item.qty = 1
        item.unit_cost = cost
        course_name = course_from_id(course_id).display_name
        item.line_desc = _("Certificate of Achievement, {mode_name} for course {course}").format(mode_name=mode_info.name,
                                                                                                 course=course_name)
        item.currency = currency
        order.currency = currency
        order.save()
        item.save()
        return item

    def purchased_callback(self):
        """
        When purchase goes through, activate and update the course enrollment for the correct mode
        """
        try:
            verification_attempt = SoftwareSecurePhotoVerification.active_for_user(self.course_enrollment.user)
            verification_attempt.submit()
        except Exception as e:
            log.exception(
                "Could not submit verification attempt for enrollment {}".format(self.course_enrollment)
            )

        self.course_enrollment.mode = self.mode
        self.course_enrollment.save()
        self.course_enrollment.activate()

    @property
    def single_item_receipt_template(self):
        if self.mode == 'verified':
            return 'shoppingcart/verified_cert_receipt.html'
        else:
            return super(CertificateItem, self).single_item_receipt_template

    @property
    def single_item_receipt_context(self):
        course = course_from_id(self.course_id)
        return {
            "course_id" : self.course_id,
            "course_name": course.display_name_with_default,
            "course_org": course.display_org_with_default,
            "course_num": course.display_number_with_default,
            "course_start_date_text": course.start_date_text,
            "course_has_started": course.start > datetime.today().replace(tzinfo=pytz.utc),
        }

    @property
    def additional_instruction_text(self):
        return _("Note - you have up to 2 weeks into the course to unenroll from the Verified Certificate option "
                 "and receive a full refund. To receive your refund, contact {billing_email}. "
                 "Please include your order number in your e-mail. "
                 "Please do NOT include your credit card information.").format(
                     billing_email=settings.PAYMENT_SUPPORT_EMAIL)
