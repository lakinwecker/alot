import os
import code
import threading
import subprocess
import shlex
import email
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import urwid
from twisted.internet import defer

from alot.commands import Command, registerCommand
from alot import buffers
from alot import settings
from alot import widgets
from alot import helper
from alot.db import DatabaseLockedError
from alot.completion import ContactsCompleter
from alot.completion import AccountCompleter
from alot.message import encode_header
from alot import commands

MODE = 'global'


@registerCommand(MODE, 'exit')
class ExitCommand(Command):
    """shuts the MUA down cleanly"""
    @defer.inlineCallbacks
    def apply(self, ui):
        if settings.config.getboolean('general', 'bug_on_exit'):
            if (yield ui.choice('realy quit?', select='yes', cancel='no',
                               msg_position='left')) == 'no':
                return
        ui.exit()


@registerCommand(MODE, 'search', arguments=[
    (['query'], {'nargs':'*', 'default':'', 'help':'search string'})]
)
class SearchCommand(Command):
    """open a new search buffer"""
    def __init__(self, query, **kwargs):
        """
        :param query: initial querystring
        """
        self.query = ' '.join(query)
        Command.__init__(self, **kwargs)

    @defer.inlineCallbacks
    def apply(self, ui):
        if self.query:
            if self.query == '*' and ui.current_buffer:
                s = 'really search for all threads? This takes a while..'
                if (yield ui.choice(s, select='yes', cancel='no')) == 'no':
                    return
            open_searches = ui.get_buffers_of_type(buffers.SearchBuffer)
            to_be_focused = None
            for sb in open_searches:
                if sb.querystring == self.query:
                    to_be_focused = sb
            if to_be_focused:
                ui.buffer_focus(to_be_focused)
            else:
                ui.buffer_open(buffers.SearchBuffer(ui, self.query))
        else:
            ui.notify('empty query string')


@registerCommand(MODE, 'prompt', arguments=[
    (['startwith'], {'nargs':'?', 'default':'', 'help':'initial content of commandprompt'})]
)
class PromptCommand(Command):
    """starts commandprompt"""
    def __init__(self, startwith='', **kwargs):
        self.startwith = startwith
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        ui.commandprompt(self.startwith)


@registerCommand(MODE, 'refresh')
class RefreshCommand(Command):
    """refreshes the current buffer"""
    def apply(self, ui):
        ui.current_buffer.rebuild()
        ui.update()


@registerCommand(MODE, 'shellescape', arguments=[
    (['cmdline'], {'help':'command line to execute'})]
)
class ExternalCommand(Command):
    """calls external command"""
    def __init__(self, commandstring, path=None, spawn=False, refocus=True,
                 in_thread=False, on_success=None, **kwargs):
        """
        :param commandstring: the command to call
        :type commandstring: str
        :param path: a path to a file (or None)
        :type path: str
        :param spawn: run command in a new terminal
        :type spawn: boolean
        :param in_thread: run asynchronously, don't block alot
        :type in_thread: boolean
        :param refocus: refocus calling buffer after cmd termination
        :type refocus: boolean
        :param on_success: code to execute after command successfully exited
        :type on_success: callable
        """
        self.commandstring = commandstring
        self.path = path
        self.spawn = spawn
        self.refocus = refocus
        self.in_thread = in_thread
        self.on_success = on_success
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        callerbuffer = ui.current_buffer

        def afterwards(data):
            if callable(self.on_success) and data == 'success':
                self.on_success()
            if self.refocus and callerbuffer in ui.buffers:
                ui.logger.info('refocussing')
                ui.buffer_focus(callerbuffer)

        write_fd = ui.mainloop.watch_pipe(afterwards)

        def thread_code(*args):
            if self.path:
                if '{}' in self.commandstring:
                    cmd = self.commandstring.replace('{}',
                            helper.shell_quote(self.path))
                else:
                    cmd = '%s %s' % (self.commandstring,
                                     helper.shell_quote(self.path))
            else:
                cmd = self.commandstring

            if self.spawn:
                cmd = '%s %s' % (settings.config.get('general',
                                                      'terminal_cmd'),
                                 cmd)
            cmd = cmd.encode('utf-8', errors='ignore')
            ui.logger.info('calling external command: %s' % cmd)
            returncode = subprocess.call(shlex.split(cmd))
            if returncode == 0:
                os.write(write_fd, 'success')

        if self.in_thread:
            thread = threading.Thread(target=thread_code)
            thread.start()
        else:
            ui.mainloop.screen.stop()
            thread_code()
            ui.mainloop.screen.start()


@registerCommand(MODE, 'edit')
class EditCommand(ExternalCommand):
    def __init__(self, path, spawn=None, **kwargs):
        self.path = path
        if spawn != None:
            self.spawn = spawn
        else:
            self.spawn = settings.config.getboolean('general', 'spawn_editor')
        editor_cmd = settings.config.get('general', 'editor_cmd')

        ExternalCommand.__init__(self, editor_cmd, path=self.path,
                                 spawn=self.spawn, in_thread=self.spawn,
                                 **kwargs)


@registerCommand(MODE, 'pyshell')
class PythonShellCommand(Command):
    """opens an interactive shell for introspection"""
    def apply(self, ui):
        ui.mainloop.screen.stop()
        code.interact(local=locals())
        ui.mainloop.screen.start()


@registerCommand(MODE, 'bclose')
@registerCommand('bufferlist', 'closefocussed', {'focussed': True})
class BufferCloseCommand(Command):
    """close a buffer"""
    def __init__(self, buffer=None, focussed=False, **kwargs):
        """
        :param buffer: the selected buffer
        :type buffer: `alot.buffers.Buffer`
        """
        self.buffer = buffer
        self.focussed = focussed
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        if self.focussed:
            #if in bufferlist, this is ugly.
            self.buffer = ui.current_buffer.get_selected_buffer()
        elif not self.buffer:
            self.buffer = ui.current_buffer
        ui.buffer_close(self.buffer)
        ui.buffer_focus(ui.current_buffer)


@registerCommand(MODE, 'bprevious', forced={'offset': -1})
@registerCommand(MODE, 'bnext', forced={'offset': +1})
@registerCommand('bufferlist', 'openfocussed')  # todo separate
class BufferFocusCommand(Command):
    """focus a buffer"""
    def __init__(self, buffer=None, offset=0, **kwargs):
        """
        :param buffer: the buffer to focus
        :type buffer: `alot.buffers.Buffer`
        """
        self.buffer = buffer
        self.offset = offset
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        if self.offset:
            idx = ui.buffers.index(ui.current_buffer)
            num = len(ui.buffers)
            self.buffer = ui.buffers[(idx + self.offset) % num]
        else:
            if not self.buffer:
                self.buffer = ui.current_buffer.get_selected_buffer()
        ui.buffer_focus(self.buffer)


@registerCommand(MODE, 'bufferlist')
class OpenBufferlistCommand(Command):
    """open a bufferlist buffer"""
    def __init__(self, filtfun=None, **kwargs):
        self.filtfun = filtfun
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        blists = ui.get_buffers_of_type(buffers.BufferlistBuffer)
        if blists:
            ui.buffer_focus(blists[0])
        else:
            ui.buffer_open(buffers.BufferlistBuffer(ui, self.filtfun))


@registerCommand(MODE, 'taglist')
class TagListCommand(Command):
    """open a taglisat buffer"""
    def __init__(self, filtfun=None, **kwargs):
        self.filtfun = filtfun
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        tags = ui.dbman.get_all_tags()
        buf = buffers.TagListBuffer(ui, tags, self.filtfun)
        ui.buffers.append(buf)
        buf.rebuild()
        ui.buffer_focus(buf)


@registerCommand(MODE, 'flush')
class FlushCommand(Command):
    """Flushes writes to the index. Retries until committed"""
    def apply(self, ui):
        try:
            ui.dbman.flush()
        except DatabaseLockedError:
            timeout = settings.config.getint('general', 'flush_retry_timeout')

            def f(*args):
                self.apply(ui)
            ui.mainloop.set_alarm_in(timeout, f)
            ui.notify('index locked, will try again in %d secs' % timeout)
            ui.update()
            return


@registerCommand(MODE, 'help', arguments=[
    (['commandname'], {'help':'command'})]
)
class HelpCommand(Command):
    def __init__(self, commandname='', **kwargs):
        Command.__init__(self, **kwargs)
        self.commandname = commandname

    def apply(self, ui):
        ui.logger.debug('HELP')
        if self.commandname:
            ui.logger.debug('HELP %s' % self.commandname)
            parser = commands.lookup_parser(self.commandname, ui.mode)
            if parser:
                ui.notify(parser.format_help())
            else:
                ui.notify('command %s not known in mode %s' % (self.commandname,
                                                              ui.mode))
            return
        else:
            # get mappings
            modemaps = dict(settings.config.items('%s-maps' % ui.mode))
            globalmaps = dict(settings.config.items('global-maps'))

            # build table
            maxkeylength = len(max((modemaps).keys() + globalmaps.keys(),
                                   key=len))
            keycolumnwidth = maxkeylength + 2

            linewidgets = []
            # mode specific maps
            linewidgets.append(urwid.Text(('helptexth1',
                                '\n%s-mode specific maps' % ui.mode)))
            for (k, v) in modemaps.items():
                line = urwid.Columns([('fixed', keycolumnwidth, urwid.Text(k)),
                                      urwid.Text(v)])
                linewidgets.append(line)

            # global maps
            linewidgets.append(urwid.Text(('helptexth1',
                                           '\nglobal maps')))
            for (k, v) in globalmaps.items():
                if k not in modemaps:
                    line = urwid.Columns(
                        [('fixed', keycolumnwidth, urwid.Text(k)),
                         urwid.Text(v)])
                    linewidgets.append(line)

            body = urwid.ListBox(linewidgets)
            ckey = 'cancel'
            titletext = 'Bindings Help (%s cancels)' % ckey

        box = widgets.DialogBox(body, titletext,
                                bodyattr='helptext',
                                titleattr='helptitle')

        # put promptwidget as overlay on main widget
        overlay = urwid.Overlay(box, ui.mainframe, 'center',
                                ('relative', 70), 'middle',
                                ('relative', 70))
        ui.show_as_root_until_keypress(overlay, 'cancel')


@registerCommand(MODE, 'compose', arguments=[
    (['--sender'], {'nargs': '?', 'help':'sender'}),
    (['--subject'], {'nargs':'?', 'help':'subject line'}),
    (['--to'], {'nargs':'+', 'help':'recipient'}),
    (['--cc'], {'nargs':'+', 'help':'copy to'}),
    (['--bcc'], {'nargs':'+', 'help':'blind copy to'}),
]
)
class ComposeCommand(Command):
    """compose a new email and open an envelope for it"""
    def __init__(self, mail=None, headers={},
                 sender=u'', subject=u'', to=[], cc=[], bcc=[],
                 **kwargs):
        Command.__init__(self, **kwargs)
        if not mail:
            self.mail = MIMEMultipart()
            self.mail.attach(MIMEText('', 'plain', 'UTF-8'))
        else:
            self.mail = mail
        for key, value in headers.items():
            self.mail[key] = encode_header(key, value)

        if sender:
            self.mail['From'] = encode_header('From', sender)
        if subject:
            self.mail['Subject'] = encode_header('Subject', subject)
        if to:
            self.mail['To'] = encode_header('To', ','.join(to))
        if cc:
            self.mail['Cc'] = encode_header('Cc', ','.join(cc))
        if bcc:
            self.mail['Bcc'] = encode_header('Bcc', ','.join(bcc))


    @defer.inlineCallbacks
    def apply(self, ui):
        # TODO: fill with default header (per account)
        # get From header
        if not 'From' in self.mail:
            accounts = ui.accountman.get_accounts()
            if len(accounts) == 0:
                ui.notify('no accounts set')
                return
            elif len(accounts) == 1:
                a = accounts[0]
            else:
                cmpl = AccountCompleter(ui.accountman)
                fromaddress = yield ui.prompt(prefix='From>', completer=cmpl,
                                              tab=1)
                validaddresses = [a.address for a in accounts] + [None]
                while fromaddress not in validaddresses:  # TODO: not cool
                    ui.notify('no account for this address. (<esc> cancels)')
                    fromaddress = yield ui.prompt(prefix='From>',
                                                  completer=cmpl)
                if not fromaddress:
                    ui.notify('canceled')
                    return
                a = ui.accountman.get_account_by_address(fromaddress)
            self.mail['From'] = "%s <%s>" % (a.realname, a.address)

        #get To header
        if 'To' not in self.mail:
            name, addr = email.Utils.parseaddr(unicode(self.mail.get('From')))
            a = ui.accountman.get_account_by_address(addr)

            allbooks = not settings.config.getboolean('general',
                                'complete_matching_abook_only')
            ui.logger.debug(allbooks)
            abooks = ui.accountman.get_addressbooks(order=[a],
                                                    append_remaining=allbooks)
            ui.logger.debug(abooks)
            to = yield ui.prompt(prefix='To>',
                                 completer=ContactsCompleter(abooks))
            if to == None:
                ui.notify('canceled')
                return
            self.mail['To'] = encode_header('to', to)
        if settings.config.getboolean('general', 'ask_subject') and \
           not 'Subject' in self.mail:
            subject = yield ui.prompt(prefix='Subject>')
            if subject == None:
                ui.notify('canceled')
                return
            self.mail['Subject'] = encode_header('subject', subject)

        ui.apply_command(commands.envelope.EnvelopeEditCommand(mail=self.mail))


@registerCommand(MODE, 'move', arguments=[
    (['key'], {'nargs':'+', 'help':'keypress to send'})]
)
@registerCommand(MODE, 'cancel', {'key': 'cancel'})
@registerCommand(MODE, 'select', {'key': 'select'})
class SendKeypressCommand(Command):
    def __init__(self, key, **kwargs):
        Command.__init__(self, **kwargs)
        if isinstance(key, list):
            key = ' '.join(key)
        self.key = key

    def apply(self, ui):
        ui.keypress(self.key)


class EnvelopeOpenCommand(Command):
    """open a new envelope buffer"""
    def __init__(self, mail=None, **kwargs):
        self.mail = mail
        Command.__init__(self, **kwargs)

    def apply(self, ui):
        ui.buffer_open(buffers.EnvelopeBuffer(ui, mail=self.mail))
