from mock import MagicMock
import datetime

from django.test import TestCase
from django.http import Http404
from django.test.utils import override_settings
from django.contrib.auth.models import User
from django.test.client import RequestFactory

from django.conf import settings
from django.core.urlresolvers import reverse

from student.models import CourseEnrollment
from student.tests.factories import AdminFactory
from mitxmako.middleware import MakoMiddleware

from xmodule.modulestore.django import modulestore, clear_existing_modulestores

import courseware.views as views
from xmodule.modulestore import Location
from pytz import UTC
from courseware.tests.modulestore_config import TEST_DATA_MIXED_MODULESTORE
from course_modes.models import CourseMode


@override_settings(MODULESTORE=TEST_DATA_MIXED_MODULESTORE)
class TestJumpTo(TestCase):
    """
        Check the jumpto link for a course.
    """

    def setUp(self):

        # Use toy course from XML
        self.course_name = 'edX/toy/2012_Fall'

    def test_jumpto_invalid_location(self):
        location = Location('i4x', 'edX', 'toy', 'NoSuchPlace', None)
        jumpto_url = '{0}/{1}/jump_to/{2}'.format('/courses', self.course_name, location)
        response = self.client.get(jumpto_url)
        self.assertEqual(response.status_code, 404)

    def test_jumpto_from_chapter(self):
        location = Location('i4x', 'edX', 'toy', 'chapter', 'Overview')
        jumpto_url = '{0}/{1}/jump_to/{2}'.format('/courses', self.course_name, location)
        expected = 'courses/edX/toy/2012_Fall/courseware/Overview/'
        response = self.client.get(jumpto_url)
        self.assertRedirects(response, expected, status_code=302, target_status_code=302)

    def test_jumpto_id(self):
        location = Location('i4x', 'edX', 'toy', 'chapter', 'Overview')
        jumpto_url = '{0}/{1}/jump_to_id/{2}'.format('/courses', self.course_name, location.name)
        expected = 'courses/edX/toy/2012_Fall/courseware/Overview/'
        response = self.client.get(jumpto_url)
        self.assertRedirects(response, expected, status_code=302, target_status_code=302)

    def test_jumpto_id_invalid_location(self):
        location = Location('i4x', 'edX', 'toy', 'NoSuchPlace', None)
        jumpto_url = '{0}/{1}/jump_to_id/{2}'.format('/courses', self.course_name, location.name)
        response = self.client.get(jumpto_url)
        self.assertEqual(response.status_code, 404)


@override_settings(MODULESTORE=TEST_DATA_MIXED_MODULESTORE)
class ViewsTestCase(TestCase):
    """ Tests for views.py methods. """
    def setUp(self):
        self.user = User.objects.create(username='dummy', password='123456',
                                        email='test@mit.edu')
        self.date = datetime.datetime(2013, 1, 22, tzinfo=UTC)
        self.course_id = 'edX/toy/2012_Fall'
        self.enrollment = CourseEnrollment.enroll(self.user, self.course_id)
        self.enrollment.created = self.date
        self.enrollment.save()
        self.location = ['tag', 'org', 'course', 'category', 'name']

        self.request_factory = RequestFactory()
        chapter = 'Overview'
        self.chapter_url = '%s/%s/%s' % ('/courses', self.course_id, chapter)

    def test_user_groups(self):
        # depreciated function
        mock_user = MagicMock()
        mock_user.is_authenticated.return_value = False
        self.assertEquals(views.user_groups(mock_user), [])

    def test_get_current_child(self):
        self.assertIsNone(views.get_current_child(MagicMock()))
        mock_xmodule = MagicMock()
        mock_xmodule.position = -1
        mock_xmodule.get_display_items.return_value = ['one', 'two']
        self.assertEquals(views.get_current_child(mock_xmodule), 'one')
        mock_xmodule_2 = MagicMock()
        mock_xmodule_2.position = 3
        mock_xmodule_2.get_display_items.return_value = []
        self.assertIsNone(views.get_current_child(mock_xmodule_2))

    def test_redirect_to_course_position(self):
        mock_module = MagicMock()
        mock_module.descriptor.id = 'Underwater Basketweaving'
        mock_module.position = 3
        mock_module.get_display_items.return_value = []
        self.assertRaises(Http404, views.redirect_to_course_position,
                          mock_module)

    def test_registered_for_course(self):
        self.assertFalse(views.registered_for_course('Basketweaving', None))
        mock_user = MagicMock()
        mock_user.is_authenticated.return_value = False
        self.assertFalse(views.registered_for_course('dummy', mock_user))
        mock_course = MagicMock()
        mock_course.id = self.course_id
        self.assertTrue(views.registered_for_course(mock_course, self.user))

    def test_jump_to_invalid(self):
        request = self.request_factory.get(self.chapter_url)
        self.assertRaisesRegexp(Http404, 'Invalid location', views.jump_to,
                                request, 'bar', ())
        self.assertRaisesRegexp(Http404, 'No data*', views.jump_to, request,
                                'dummy', self.location)

    def test_no_end_on_about_page(self):
        # Toy course has no course end date or about/end_date blob
        self.verify_end_date('edX/toy/TT_2012_Fall')

    def test_no_end_about_blob(self):
        # test_end has a course end date, no end_date HTML blob
        self.verify_end_date("edX/test_end/2012_Fall", "Sep 17, 2015")

    def test_about_blob_end_date(self):
        # test_about_blob_end_date has both a course end date and an end_date HTML blob.
        # HTML blob wins
        self.verify_end_date("edX/test_about_blob_end_date/2012_Fall", "Learning never ends")

    def verify_end_date(self, course_id, expected_end_text=None):
        request = self.request_factory.get("foo")
        request.user = self.user

        # TODO: Remove the dependency on MakoMiddleware (by making the views explicitly supply a RequestContext)
        MakoMiddleware().process_request(request)

        result = views.course_about(request, course_id)
        if expected_end_text is not None:
            self.assertContains(result, "Classes End")
            self.assertContains(result, expected_end_text)
        else:
            self.assertNotContains(result, "Classes End")

    def test_chat_settings(self):
        mock_user = MagicMock()
        mock_user.username = "johndoe"

        mock_course = MagicMock()
        mock_course.id = "a/b/c"

        # Stub this out in the case that it's not in the settings
        domain = "jabber.edx.org"
        settings.JABBER_DOMAIN = domain

        chat_settings = views.chat_settings(mock_course, mock_user)

        # Test the proper format of all chat settings
        self.assertEquals(chat_settings['domain'], domain)
        self.assertEquals(chat_settings['room'], "a-b-c_class")
        self.assertEquals(chat_settings['username'], "johndoe@%s" % domain)

        # TODO: this needs to be changed once we figure out how to
        #       generate/store a real password.
        self.assertEquals(chat_settings['password'], "johndoe@%s" % domain)

    def test_course_mktg_about_coming_soon(self):
        # we should not be able to find this course
        url = reverse('mktg_about_course', kwargs={'course_id': 'no/course/here'})
        response = self.client.get(url)
        self.assertIn('Coming Soon', response.content)

    def test_course_mktg_register(self):
        admin = AdminFactory()
        self.client.login(username=admin.username, password='test')
        url = reverse('mktg_about_course', kwargs={'course_id': self.course_id})
        response = self.client.get(url)
        self.assertIn('Register for', response.content)
        self.assertNotIn('and choose your student track', response.content)

    def test_course_mktg_register_multiple_modes(self):
        admin = AdminFactory()
        CourseMode.objects.get_or_create(mode_slug='honor',
                                         mode_display_name='Honor Code Certificate',
                                         course_id=self.course_id)
        CourseMode.objects.get_or_create(mode_slug='verified',
                                         mode_display_name='Verified Certificate',
                                         course_id=self.course_id)
        self.client.login(username=admin.username, password='test')
        url = reverse('mktg_about_course', kwargs={'course_id': self.course_id})
        response = self.client.get(url)
        self.assertIn('Register for', response.content)
        self.assertIn('and choose your student track', response.content)
        # clean up course modes
        CourseMode.objects.all().delete()

    def test_submission_history_xss(self):
        # log into a staff account
        admin = AdminFactory()

        self.client.login(username=admin.username, password='test')

        # try it with an existing user and a malicious location
        url = reverse('submission_history', kwargs={
            'course_id': self.course_id,
            'student_username': 'dummy',
            'location': '<script>alert("hello");</script>'
        })
        response = self.client.get(url)
        self.assertFalse('<script>' in response.content)

        # try it with a malicious user and a non-existent location
        url = reverse('submission_history', kwargs={
            'course_id': self.course_id,
            'student_username': '<script>alert("hello");</script>',
            'location': 'dummy'
        })
        response = self.client.get(url)
        self.assertFalse('<script>' in response.content)

    def test_accordion_due_date(self):
        """
        Tests the formatting of due dates in the accordion view.
        """
        def get_accordion():
            """ Returns the HTML for the accordion """
            return views.render_accordion(
                request, modulestore().get_course("edX/due_date/2013_fall"),
                "c804fa32227142a1bd9d5bc183d4a20d", None, None
            )

        request = self.request_factory.get("foo")
        self.verify_due_date(request, get_accordion)

    def test_progress_due_date(self):
        """
        Tests the formatting of due dates in the progress page.
        """
        def get_progress():
            """ Returns the HTML for the progress page """
            return views.progress(request, "edX/due_date/2013_fall", self.user.id).content

        request = self.request_factory.get("foo")
        self.verify_due_date(request, get_progress)

    def verify_due_date(self, request, get_text):
        """
        Verifies that due dates are formatted properly in text returned by get_text function.
        """
        def set_show_timezone(show_timezone):
            """
            Sets the show_timezone property and returns value from get_text function.

            Note that show_timezone is deprecated and cannot be set by the user.
            """
            course.show_timezone = show_timezone
            course.save()
            return get_text()

        def set_due_date_format(due_date_format):
            """
            Sets the due_date_display_format property and returns value from get_text function.
            """
            course.due_date_display_format = due_date_format
            course.save()
            return get_text()

        request.user = self.user
        # Clear out the modulestores, so we start with the test course in its default state.
        clear_existing_modulestores()
        course = modulestore().get_course("edX/due_date/2013_fall")

        time_with_utc = "due Sep 18, 2013 at 11:30 UTC"
        time_without_utc = "due Sep 18, 2013 at 11:30"

        # The test course being used has show_timezone = False in the policy file
        # (and no due_date_display_format set). This is to test our backwards compatibility--
        # in course_module's init method, the date_display_format will be set accordingly to
        # remove the timezone.
        text = get_text()
        self.assertIn(time_without_utc, text)
        self.assertNotIn(time_with_utc, text)
        # Test that show_timezone has been cleared (which means you get the default value of True).
        self.assertTrue(course.show_timezone)

        # Clear out the due date format and verify you get the default (with timezone).
        delattr(course, 'due_date_display_format')
        course.save()
        text = get_text()
        self.assertIn(time_with_utc, text)

        # Same for setting the due date to None
        text = set_due_date_format(None)
        self.assertIn(time_with_utc, text)

        # plain text due date
        text = set_due_date_format("foobar")
        self.assertNotIn(time_with_utc, text)
        self.assertIn("due foobar", text)

        # due date with no time
        text = set_due_date_format(u"%b %d %y")
        self.assertNotIn(time_with_utc, text)
        self.assertIn("due Sep 18 13", text)

        # hide due date completely
        text = set_due_date_format(u"")
        self.assertNotIn("due ", text)

        # improperly formatted due_date_display_format falls through to default
        # (value of show_timezone does not matter-- setting to False to make that clear).
        set_show_timezone(False)
        text = set_due_date_format(u"%%%")
        self.assertNotIn("%%%", text)
        self.assertIn(time_with_utc, text)
