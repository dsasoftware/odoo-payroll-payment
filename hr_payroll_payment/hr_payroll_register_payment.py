# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
from werkzeug import url_encode


class HrPayslip(models.Model):
    _inherit = 'hr.payslip'

    @api.multi
    @api.depends('move_id', 'move_id.line_ids.full_reconcile_id')
    def _is_paid(self):
        for payslip in self:
            payslip.is_paid = (
                payslip.move_id and len(payslip.move_id.line_ids.filtered(lambda l: (
                    l.partner_id.id == payslip.employee_id.address_home_id.id and
                    l.account_id.internal_type == 'payable' and
                    not l.reconciled
                ))) == 0
            )

    is_paid = fields.Boolean(string="Has been Paid", compute='_is_paid', store=True)


class HrPayrollRegisterPaymentWizard(models.TransientModel):

    _name = "hr.payroll.register.payment.wizard"
    _description = "Hr Payroll Register Payment wizard"

    @api.model
    def _default_partner_id(self):
        context = dict(self._context or {})
        active_ids = context.get('active_ids', [])
        payslip = self.env['hr.payslip'].browse(active_ids)
        return payslip.employee_id.address_home_id.id

    @api.model
    def _default_amount(self):
        context = dict(self._context or {})
        active_ids = context.get('active_ids', [])
        payslip = self.env['hr.payslip'].browse(active_ids)
        amount = 0.0
        for line in payslip.move_id.line_ids:
            if line.account_id.internal_type == 'payable' and line.partner_id.id == payslip.employee_id.address_home_id.id:
                amount += abs(line.balance)
        return amount

    @api.model
    def _default_communication(self):
        context = dict(self._context or {})
        active_ids = context.get('active_ids', [])
        payslip = self.env['hr.payslip'].browse(active_ids)
        return payslip.number

    partner_id = fields.Many2one('res.partner', string='Partner', required=True, default=_default_partner_id)
    journal_id = fields.Many2one('account.journal', string='Payment Method', required=True, domain=[('type', 'in', ('bank', 'cash'))])
    company_id = fields.Many2one('res.company', related='journal_id.company_id', string='Company', readonly=True, required=True)
    payment_method_id = fields.Many2one('account.payment.method', string='Payment Type', required=True)
    amount = fields.Monetary(string='Payment Amount', required=True, default=_default_amount)
    currency_id = fields.Many2one('res.currency', string='Currency', required=True, default=lambda self: self.env.user.company_id.currency_id)
    payment_date = fields.Date(string='Payment Date', default=fields.Date.context_today, required=True)
    communication = fields.Char(string='Memo', default=_default_communication)
    hide_payment_method = fields.Boolean(compute='_compute_hide_payment_method',
        help="Technical field used to hide the payment method if the selected journal has only one available which is 'manual'")

    @api.one
    @api.constrains('amount')
    def _check_amount(self):
        if not self.amount > 0.0:
            raise ValidationError('The payment amount must be strictly positive.')

    @api.one
    @api.depends('journal_id')
    def _compute_hide_payment_method(self):
        if not self.journal_id:
            self.hide_payment_method = True
            return
        journal_payment_methods = self.journal_id.outbound_payment_method_ids
        self.hide_payment_method = len(journal_payment_methods) == 1 and journal_payment_methods[0].code == 'manual'

    @api.onchange('journal_id')
    def _onchange_journal(self):
        if self.journal_id:
            # Set default payment method (we consider the first to be the default one)
            payment_methods = self.journal_id.outbound_payment_method_ids
            self.payment_method_id = payment_methods and payment_methods[0] or False
            # Set payment method domain (restrict to methods enabled for the journal and to selected payment type)
            return {'domain': {'payment_method_id': [('payment_type', '=', 'outbound'), ('id', 'in', payment_methods.ids)]}}
        return {}

    @api.multi
    def payroll_post_payment(self):
        self.ensure_one()
        context = dict(self._context or {})
        active_ids = context.get('active_ids', [])
        payslip = self.env['hr.payslip'].browse(active_ids)

        # Create payment and post it
        payment = self.env['account.payment'].create({
            'partner_type': 'supplier',
            'payment_type': 'outbound',
            'partner_id': self.partner_id.id,
            'journal_id': self.journal_id.id,
            'company_id': self.company_id.id,
            'payment_method_id': self.payment_method_id.id,
            'amount': self.amount,
            'currency_id': self.currency_id.id,
            'payment_date': self.payment_date,
            'communication': self.communication
        })
        payment.post()

        # Log the payment in the chatter
        #body = (_("A payment of %s %s with the reference <a href='/mail/view?%s'>%s</a> related to your payslip %s has been made.") % (payment.amount, payment.currency_id.symbol, url_encode({'model': 'account.payment', 'res_id': payment.id}), payment.name, payroll_sheet.name))
        #payroll_sheet.message_post(body=body)

        # Reconcile the payment and the payroll, i.e. lookup on the payable account move lines
        account_move_lines_to_reconcile = self.env['account.move.line']
        for line in payment.move_line_ids:
            if line.account_id.internal_type == 'payable':
                account_move_lines_to_reconcile |= line
        for line in payslip.move_id.line_ids:
            if line.account_id.internal_type == 'payable' and line.partner_id.id == self.partner_id.id:
                account_move_lines_to_reconcile |= line

        account_move_lines_to_reconcile.reconcile()

        return {'type': 'ir.actions.act_window_close'}
