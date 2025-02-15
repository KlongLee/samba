# Integration tests for pycredentials
#
# Copyright (C) Catalyst IT Ltd. 2017
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
from samba.tests import TestCase, delete_force
import os

import ldb

import samba
from samba.auth import system_session
from samba.credentials import (
    Credentials,
    CLI_CRED_NTLMv2_AUTH,
    CLI_CRED_NTLM_AUTH,
    DONT_USE_KERBEROS)
from samba.dcerpc import lsa, netlogon, ntlmssp, security, srvsvc
from samba.dcerpc.netlogon import (
    netr_Authenticator,
    netr_WorkstationInformation,
    MSV1_0_ALLOW_MSVCHAPV2
)
from samba.dcerpc.misc import SEC_CHAN_WKSTA
from samba.dsdb import (
    UF_WORKSTATION_TRUST_ACCOUNT,
    UF_PASSWD_NOTREQD,
    UF_NORMAL_ACCOUNT)
from samba.ndr import ndr_pack, ndr_unpack
from samba.samdb import SamDB
from samba import NTSTATUSError, ntstatus
from samba.common import get_string
from samba.sd_utils import SDUtils

import ctypes


"""
Integration tests for pycredentials
"""

MACHINE_NAME = "PCTM"
USER_NAME    = "PCTU"

class PyCredentialsTests(TestCase):

    def setUp(self):
        super().setUp()

        self.server      = os.environ["SERVER"]
        self.domain      = os.environ["DOMAIN"]
        self.host        = os.environ["SERVER_IP"]
        self.lp          = self.get_loadparm()

        self.credentials = self.get_credentials()

        self.session     = system_session()
        self.ldb = SamDB(url="ldap://%s" % self.host,
                         session_info=self.session,
                         credentials=self.credentials,
                         lp=self.lp)

        self.create_machine_account()
        self.create_user_account()

    def tearDown(self):
        super().tearDown()
        delete_force(self.ldb, self.machine_dn)
        delete_force(self.ldb, self.user_dn)

    # Until a successful netlogon connection has been established there will
    # not be a valid authenticator associated with the credentials
    # and new_client_authenticator should throw a ValueError
    def test_no_netlogon_connection(self):
        self.assertRaises(ValueError,
                          self.machine_creds.new_client_authenticator)

    # Once a netlogon connection has been established,
    # new_client_authenticator should return a value
    #
    def test_have_netlogon_connection(self):
        c = self.get_netlogon_connection()
        a = self.machine_creds.new_client_authenticator()
        self.assertIsNotNone(a)

    # Get an authenticator and use it on a sequence of operations requiring
    # an authenticator
    def test_client_authenticator(self):
        c = self.get_netlogon_connection()
        (authenticator, subsequent) = self.get_authenticator()
        self.do_NetrLogonSamLogonWithFlags(c, authenticator, subsequent)
        (authenticator, subsequent) = self.get_authenticator()
        self.do_NetrLogonGetDomainInfo(c, authenticator, subsequent)
        (authenticator, subsequent) = self.get_authenticator()
        self.do_NetrLogonGetDomainInfo(c, authenticator, subsequent)
        (authenticator, subsequent) = self.get_authenticator()
        self.do_NetrLogonGetDomainInfo(c, authenticator, subsequent)

    # Test using LogonGetDomainInfo to update dNSHostName to an allowed value.
    def test_set_dns_hostname_valid(self):
        c = self.get_netlogon_connection()
        authenticator, subsequent = self.get_authenticator()

        domain_hostname = self.ldb.domain_dns_name()

        new_dns_hostname = f'{self.machine_name}.{domain_hostname}'
        new_dns_hostname = new_dns_hostname.encode('utf-8')

        query = netr_WorkstationInformation()
        query.os_name = lsa.String('some OS')
        query.dns_hostname = new_dns_hostname

        c.netr_LogonGetDomainInfo(
            server_name=self.server,
            computer_name=self.user_creds.get_workstation(),
            credential=authenticator,
            return_authenticator=subsequent,
            level=1,
            query=query)

        # Check the result.

        res = self.ldb.search(self.machine_dn,
                              scope=ldb.SCOPE_BASE,
                              attrs=['dNSHostName'])
        self.assertEqual(1, len(res))

        got_dns_hostname = res[0].get('dNSHostName', idx=0)
        self.assertEqual(new_dns_hostname, got_dns_hostname)

    # Test using LogonGetDomainInfo to update dNSHostName to an allowed value,
    # when we are denied the right to do so.
    def test_set_dns_hostname_valid_denied(self):
        c = self.get_netlogon_connection()
        authenticator, subsequent = self.get_authenticator()

        res = self.ldb.search(self.machine_dn,
                              scope=ldb.SCOPE_BASE,
                              attrs=['objectSid'])
        self.assertEqual(1, len(res))

        machine_sid = ndr_unpack(security.dom_sid,
                                 res[0].get('objectSid', idx=0))

        sd_utils = SDUtils(self.ldb)

        # Deny Validated Write and Write Property.
        mod = (f'(OD;;SWWP;{security.GUID_DRS_DNS_HOST_NAME};;'
               f'{machine_sid})')
        sd_utils.dacl_add_ace(self.machine_dn, mod)

        domain_hostname = self.ldb.domain_dns_name()

        new_dns_hostname = f'{self.machine_name}.{domain_hostname}'
        new_dns_hostname = new_dns_hostname.encode('utf-8')

        query = netr_WorkstationInformation()
        query.os_name = lsa.String('some OS')
        query.dns_hostname = new_dns_hostname

        c.netr_LogonGetDomainInfo(
            server_name=self.server,
            computer_name=self.user_creds.get_workstation(),
            credential=authenticator,
            return_authenticator=subsequent,
            level=1,
            query=query)

        # Check the result.

        res = self.ldb.search(self.machine_dn,
                              scope=ldb.SCOPE_BASE,
                              attrs=['dNSHostName'])
        self.assertEqual(1, len(res))

        got_dns_hostname = res[0].get('dNSHostName', idx=0)
        self.assertEqual(new_dns_hostname, got_dns_hostname)

    # Ensure we can't use LogonGetDomainInfo to update dNSHostName to an
    # invalid value, even with Validated Write.
    def test_set_dns_hostname_invalid_validated_write(self):
        c = self.get_netlogon_connection()
        authenticator, subsequent = self.get_authenticator()

        res = self.ldb.search(self.machine_dn,
                              scope=ldb.SCOPE_BASE,
                              attrs=['objectSid'])
        self.assertEqual(1, len(res))

        machine_sid = ndr_unpack(security.dom_sid,
                                 res[0].get('objectSid', idx=0))

        sd_utils = SDUtils(self.ldb)

        # Grant Validated Write.
        mod = (f'(OA;;SW;{security.GUID_DRS_DNS_HOST_NAME};;'
               f'{machine_sid})')
        sd_utils.dacl_add_ace(self.machine_dn, mod)

        new_dns_hostname = b'invalid'

        query = netr_WorkstationInformation()
        query.os_name = lsa.String('some OS')
        query.dns_hostname = new_dns_hostname

        c.netr_LogonGetDomainInfo(
            server_name=self.server,
            computer_name=self.user_creds.get_workstation(),
            credential=authenticator,
            return_authenticator=subsequent,
            level=1,
            query=query)

        # Check the result.

        res = self.ldb.search(self.machine_dn,
                              scope=ldb.SCOPE_BASE,
                              attrs=['dNSHostName'])
        self.assertEqual(1, len(res))

        got_dns_hostname = res[0].get('dNSHostName', idx=0)
        self.assertIsNone(got_dns_hostname)

    # Ensure we can't use LogonGetDomainInfo to update dNSHostName to an
    # invalid value, even with Write Property.
    def test_set_dns_hostname_invalid_write_property(self):
        c = self.get_netlogon_connection()
        authenticator, subsequent = self.get_authenticator()

        res = self.ldb.search(self.machine_dn,
                              scope=ldb.SCOPE_BASE,
                              attrs=['objectSid'])
        self.assertEqual(1, len(res))

        machine_sid = ndr_unpack(security.dom_sid,
                                 res[0].get('objectSid', idx=0))

        sd_utils = SDUtils(self.ldb)

        # Grant Write Property.
        mod = (f'(OA;;WP;{security.GUID_DRS_DNS_HOST_NAME};;'
               f'{machine_sid})')
        sd_utils.dacl_add_ace(self.machine_dn, mod)

        new_dns_hostname = b'invalid'

        query = netr_WorkstationInformation()
        query.os_name = lsa.String('some OS')
        query.dns_hostname = new_dns_hostname

        c.netr_LogonGetDomainInfo(
            server_name=self.server,
            computer_name=self.user_creds.get_workstation(),
            credential=authenticator,
            return_authenticator=subsequent,
            level=1,
            query=query)

        # Check the result.

        res = self.ldb.search(self.machine_dn,
                              scope=ldb.SCOPE_BASE,
                              attrs=['dNSHostName'])
        self.assertEqual(1, len(res))

        got_dns_hostname = res[0].get('dNSHostName', idx=0)
        self.assertIsNone(got_dns_hostname)

    # Show we can't use LogonGetDomainInfo to set the dNSHostName to just the
    # machine name.
    def test_set_dns_hostname_to_machine_name(self):
        c = self.get_netlogon_connection()
        authenticator, subsequent = self.get_authenticator()

        new_dns_hostname = self.machine_name.encode('utf-8')

        query = netr_WorkstationInformation()
        query.os_name = lsa.String('some OS')
        query.dns_hostname = new_dns_hostname

        c.netr_LogonGetDomainInfo(
            server_name=self.server,
            computer_name=self.user_creds.get_workstation(),
            credential=authenticator,
            return_authenticator=subsequent,
            level=1,
            query=query)

        # Check the result.

        res = self.ldb.search(self.machine_dn,
                              scope=ldb.SCOPE_BASE,
                              attrs=['dNSHostName'])
        self.assertEqual(1, len(res))

        got_dns_hostname = res[0].get('dNSHostName', idx=0)
        self.assertIsNone(got_dns_hostname)

    # Show we can't use LogonGetDomainInfo to set dNSHostName with an invalid
    # suffix.
    def test_set_dns_hostname_invalid_suffix(self):
        c = self.get_netlogon_connection()
        authenticator, subsequent = self.get_authenticator()

        domain_hostname = self.ldb.domain_dns_name()

        new_dns_hostname = f'{self.machine_name}.foo.{domain_hostname}'
        new_dns_hostname = new_dns_hostname.encode('utf-8')

        query = netr_WorkstationInformation()
        query.os_name = lsa.String('some OS')
        query.dns_hostname = new_dns_hostname

        c.netr_LogonGetDomainInfo(
            server_name=self.server,
            computer_name=self.user_creds.get_workstation(),
            credential=authenticator,
            return_authenticator=subsequent,
            level=1,
            query=query)

        # Check the result.

        res = self.ldb.search(self.machine_dn,
                              scope=ldb.SCOPE_BASE,
                              attrs=['dNSHostName'])
        self.assertEqual(1, len(res))

        got_dns_hostname = res[0].get('dNSHostName', idx=0)
        self.assertIsNone(got_dns_hostname)

    # Test that setting the HANDLES_SPN_UPDATE flag inhibits the dNSHostName
    # update, but other attributes are still updated.
    def test_set_dns_hostname_with_flag(self):
        c = self.get_netlogon_connection()
        authenticator, subsequent = self.get_authenticator()

        domain_hostname = self.ldb.domain_dns_name()

        new_dns_hostname = f'{self.machine_name}.{domain_hostname}'
        new_dns_hostname = new_dns_hostname.encode('utf-8')

        operating_system = 'some OS'

        query = netr_WorkstationInformation()
        query.os_name = lsa.String(operating_system)

        query.dns_hostname = new_dns_hostname
        query.workstation_flags = netlogon.NETR_WS_FLAG_HANDLES_SPN_UPDATE

        c.netr_LogonGetDomainInfo(
            server_name=self.server,
            computer_name=self.user_creds.get_workstation(),
            credential=authenticator,
            return_authenticator=subsequent,
            level=1,
            query=query)

        # Check the result.

        res = self.ldb.search(self.machine_dn,
                              scope=ldb.SCOPE_BASE,
                              attrs=['dNSHostName',
                                     'operatingSystem'])
        self.assertEqual(1, len(res))

        got_dns_hostname = res[0].get('dNSHostName', idx=0)
        self.assertIsNone(got_dns_hostname)

        got_os = res[0].get('operatingSystem', idx=0)
        self.assertEqual(operating_system.encode('utf-8'), got_os)

    def test_SamLogonEx(self):
        c = self.get_netlogon_connection()

        logon = samlogon_logon_info(self.domain,
                                    self.machine_name,
                                    self.user_creds)

        logon_level = netlogon.NetlogonNetworkTransitiveInformation
        validation_level = netlogon.NetlogonValidationSamInfo4
        netr_flags = 0

        try:
            c.netr_LogonSamLogonEx(self.server,
                                   self.user_creds.get_workstation(),
                                   logon_level,
                                   logon,
                                   validation_level,
                                   netr_flags)
        except NTSTATUSError as e:
            enum = ctypes.c_uint32(e.args[0]).value
            if enum == ntstatus.NT_STATUS_WRONG_PASSWORD:
                self.fail("got wrong password error")
            else:
                raise

    def test_SamLogonEx_no_domain(self):
        c = self.get_netlogon_connection()

        self.user_creds.set_domain('')

        logon = samlogon_logon_info(self.domain,
                                    self.machine_name,
                                    self.user_creds)

        logon_level = netlogon.NetlogonNetworkTransitiveInformation
        validation_level = netlogon.NetlogonValidationSamInfo4
        netr_flags = 0

        try:
            c.netr_LogonSamLogonEx(self.server,
                                   self.user_creds.get_workstation(),
                                   logon_level,
                                   logon,
                                   validation_level,
                                   netr_flags)
        except NTSTATUSError as e:
            enum = ctypes.c_uint32(e.args[0]).value
            if enum == ntstatus.NT_STATUS_WRONG_PASSWORD:
                self.fail("got wrong password error")
            else:
                self.fail("got unexpected error" + str(e))

    def test_SamLogonExNTLM(self):
        c = self.get_netlogon_connection()

        logon = samlogon_logon_info(self.domain,
                                    self.machine_name,
                                    self.user_creds,
                                    flags=CLI_CRED_NTLM_AUTH)

        logon_level = netlogon.NetlogonNetworkTransitiveInformation
        validation_level = netlogon.NetlogonValidationSamInfo4
        netr_flags = 0

        try:
            c.netr_LogonSamLogonEx(self.server,
                                   self.user_creds.get_workstation(),
                                   logon_level,
                                   logon,
                                   validation_level,
                                   netr_flags)
        except NTSTATUSError as e:
            enum = ctypes.c_uint32(e.args[0]).value
            if enum == ntstatus.NT_STATUS_WRONG_PASSWORD:
                self.fail("got wrong password error")
            else:
                raise

    def test_SamLogonExMSCHAPv2(self):
        c = self.get_netlogon_connection()

        logon = samlogon_logon_info(self.domain,
                                    self.machine_name,
                                    self.user_creds,
                                    flags=CLI_CRED_NTLM_AUTH)

        logon.identity_info.parameter_control = MSV1_0_ALLOW_MSVCHAPV2

        logon_level = netlogon.NetlogonNetworkTransitiveInformation
        validation_level = netlogon.NetlogonValidationSamInfo4
        netr_flags = 0

        try:
            c.netr_LogonSamLogonEx(self.server,
                                   self.user_creds.get_workstation(),
                                   logon_level,
                                   logon,
                                   validation_level,
                                   netr_flags)
        except NTSTATUSError as e:
            enum = ctypes.c_uint32(e.args[0]).value
            if enum == ntstatus.NT_STATUS_WRONG_PASSWORD:
                self.fail("got wrong password error")
            else:
                raise

    # Test Credentials.encrypt_netr_crypt_password
    # By performing a NetrServerPasswordSet2
    # And the logging on using the new password.

    def test_encrypt_netr_password(self):
        # Change the password
        self.do_Netr_ServerPasswordSet2()
        # Now use the new password to perform an operation
        srvsvc.srvsvc("ncacn_np:%s" % (self.server),
                      self.lp,
                      self.machine_creds)

   # Change the current machine account password with a
   # netr_ServerPasswordSet2 call.

    def do_Netr_ServerPasswordSet2(self):
        c = self.get_netlogon_connection()
        (authenticator, subsequent) = self.get_authenticator()
        PWD_LEN  = 32
        DATA_LEN = 512
        newpass = samba.generate_random_password(PWD_LEN, PWD_LEN)
        encoded = newpass.encode('utf-16-le')
        pwd_len = len(encoded)
        filler  = [x if isinstance(x, int) else ord(x) for x in os.urandom(DATA_LEN - pwd_len)]
        pwd = netlogon.netr_CryptPassword()
        pwd.length = pwd_len
        pwd.data = filler + [x if isinstance(x, int) else ord(x) for x in encoded]
        self.machine_creds.encrypt_netr_crypt_password(pwd)
        c.netr_ServerPasswordSet2(self.server,
                                  f'{self.machine_name}$',
                                  SEC_CHAN_WKSTA,
                                  self.machine_creds.get_workstation(),
                                  authenticator,
                                  pwd)

        self.machine_pass = newpass
        self.machine_creds.set_password(newpass)

    # Establish sealed schannel netlogon connection over TCP/IP
    #
    def get_netlogon_connection(self):
        return netlogon.netlogon("ncacn_ip_tcp:%s[schannel,seal]" % self.server,
                                 self.lp,
                                 self.machine_creds)

    #
    # Create the machine account
    def create_machine_account(self):
        self.machine_pass = samba.generate_random_password(32, 32)
        self.machine_name = MACHINE_NAME
        self.machine_dn = "cn=%s,%s" % (self.machine_name, self.ldb.domain_dn())

        # remove the account if it exists, this will happen if a previous test
        # run failed
        delete_force(self.ldb, self.machine_dn)

        utf16pw = ('"%s"' % get_string(self.machine_pass)).encode('utf-16-le')
        self.ldb.add({
            "dn": self.machine_dn,
            "objectclass": "computer",
            "sAMAccountName": "%s$" % self.machine_name,
            "userAccountControl":
                str(UF_WORKSTATION_TRUST_ACCOUNT | UF_PASSWD_NOTREQD),
            "unicodePwd": utf16pw})

        self.machine_creds = Credentials()
        self.machine_creds.guess(self.get_loadparm())
        self.machine_creds.set_secure_channel_type(SEC_CHAN_WKSTA)
        self.machine_creds.set_kerberos_state(DONT_USE_KERBEROS)
        self.machine_creds.set_password(self.machine_pass)
        self.machine_creds.set_username(self.machine_name + "$")
        self.machine_creds.set_workstation(self.machine_name)

    #
    # Create a test user account
    def create_user_account(self):
        self.user_pass = samba.generate_random_password(32, 32)
        self.user_name = USER_NAME
        self.user_dn = "cn=%s,%s" % (self.user_name, self.ldb.domain_dn())

        # remove the account if it exists, this will happen if a previous test
        # run failed
        delete_force(self.ldb, self.user_dn)

        utf16pw = ('"%s"' % get_string(self.user_pass)).encode('utf-16-le')
        self.ldb.add({
            "dn": self.user_dn,
            "objectclass": "user",
            "sAMAccountName": "%s" % self.user_name,
            "userAccountControl": str(UF_NORMAL_ACCOUNT),
            "unicodePwd": utf16pw})

        self.user_creds = Credentials()
        self.user_creds.guess(self.get_loadparm())
        self.user_creds.set_password(self.user_pass)
        self.user_creds.set_username(self.user_name)
        self.user_creds.set_workstation(self.machine_name)

    #
    # Get the authenticator from the machine creds.
    def get_authenticator(self):
        auth = self.machine_creds.new_client_authenticator()
        current = netr_Authenticator()
        current.cred.data = [x if isinstance(x, int) else ord(x) for x in auth["credential"]]
        current.timestamp = auth["timestamp"]

        subsequent = netr_Authenticator()
        return (current, subsequent)

    def do_NetrLogonSamLogonWithFlags(self, c, current, subsequent):
        logon = samlogon_logon_info(self.domain,
                                    self.machine_name,
                                    self.user_creds)

        logon_level = netlogon.NetlogonNetworkTransitiveInformation
        validation_level = netlogon.NetlogonValidationSamInfo4
        netr_flags = 0
        c.netr_LogonSamLogonWithFlags(self.server,
                                      self.user_creds.get_workstation(),
                                      current,
                                      subsequent,
                                      logon_level,
                                      logon,
                                      validation_level,
                                      netr_flags)

    def do_NetrLogonGetDomainInfo(self, c, current, subsequent):
        query = netr_WorkstationInformation()

        c.netr_LogonGetDomainInfo(self.server,
                                  self.user_creds.get_workstation(),
                                  current,
                                  subsequent,
                                  2,
                                  query)

#
# Build the logon data required by NetrLogonSamLogonWithFlags


def samlogon_logon_info(domain_name, computer_name, creds,
                        flags=CLI_CRED_NTLMv2_AUTH):

    target_info_blob = samlogon_target(domain_name, computer_name)

    challenge = b"abcdefgh"
    # User account under test
    response = creds.get_ntlm_response(flags=flags,
                                       challenge=challenge,
                                       target_info=target_info_blob)

    logon = netlogon.netr_NetworkInfo()

    logon.challenge     = [x if isinstance(x, int) else ord(x) for x in challenge]
    logon.nt            = netlogon.netr_ChallengeResponse()
    logon.nt.length     = len(response["nt_response"])
    logon.nt.data       = [x if isinstance(x, int) else ord(x) for x in response["nt_response"]]
    logon.identity_info = netlogon.netr_IdentityInfo()

    (username, domain)  = creds.get_ntlm_username_domain()
    logon.identity_info.domain_name.string  = domain
    logon.identity_info.account_name.string = username
    logon.identity_info.workstation.string  = creds.get_workstation()

    return logon

#
# Build the samlogon target info.


def samlogon_target(domain_name, computer_name):
    target_info = ntlmssp.AV_PAIR_LIST()
    target_info.count = 3
    computername = ntlmssp.AV_PAIR()
    computername.AvId = ntlmssp.MsvAvNbComputerName
    computername.Value = computer_name

    domainname = ntlmssp.AV_PAIR()
    domainname.AvId = ntlmssp.MsvAvNbDomainName
    domainname.Value = domain_name

    eol = ntlmssp.AV_PAIR()
    eol.AvId = ntlmssp.MsvAvEOL
    target_info.pair = [domainname, computername, eol]

    return ndr_pack(target_info)
