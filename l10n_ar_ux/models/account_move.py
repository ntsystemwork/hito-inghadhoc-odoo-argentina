##############################################################################
# For copyright and license notices, see __manifest__.py file in module root
# directory
##############################################################################
from odoo import models, fields, api, _
from odoo.exceptions import ValidationError
import re
import logging
_logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _inherit = "account.move"

    computed_currency_rate = fields.Float(
        compute='_compute_currency_rate',
        string='Currency Rate (preview)',
        digits=(16, 6),
    )
    l10n_ar_currency_rate = fields.Float(compute='_compute_l10n_ar_currency_rate', store=True)

    @api.depends('reversed_entry_id')
    def _compute_l10n_ar_currency_rate(self):
        """ If it's a credit note on foreign currency and foreing currency is the same as original credit note, then
        we use original invoice rate """
        ar_reversed_other_currency = self.filtered(
            lambda x: x.is_invoice() and x.reversed_entry_id and
            x.company_id.country_id == self.env.ref('base.ar') and
            x.currency_id != x.company_id.currency_id and
            x.reversed_entry_id.currency_id == x.currency_id)
        self.filtered(lambda x: x.move_type == 'entry').l10n_ar_currency_rate = False
        for rec in ar_reversed_other_currency:
            rec.l10n_ar_currency_rate = rec.reversed_entry_id.l10n_ar_currency_rate

    @api.depends('currency_id', 'company_id', 'invoice_date')
    def _compute_currency_rate(self):
        for rec in self:
            if rec.currency_id and rec.company_id and (rec.currency_id != rec.company_id.currency_id):
                rec.computed_currency_rate = rec.currency_id._convert(
                    1.0, rec.company_id.currency_id, rec.company_id,
                    date=rec.invoice_date or fields.Date.context_today(rec),
                    round=False)
            else:
                rec.computed_currency_rate = 1.0

    @api.model
    def _l10n_ar_get_document_number_parts(self, document_number, document_type_code):
        """
        For compatibility with old invoices/documents we replicate part of previous method
        https://github.com/ingadhoc/odoo-argentina/blob/12.0/l10n_ar_account/models/account_invoice.py#L234
        """
        try:
            return super()._l10n_ar_get_document_number_parts(document_number, document_type_code)
        except Exception:
            _logger.info('Error while getting document number parts, try with backward compatibility')
        invoice_number = point_of_sale = False
        if document_type_code in ['33', '99', '331', '332']:
            point_of_sale = '0'
            # leave only numbers and convert to integer
            # otherwise use date as a number
            if re.search(r'\d', document_number):
                invoice_number = document_number
        elif "-" in document_number:
            splited_number = document_number.split('-')
            invoice_number = splited_number.pop()
            point_of_sale = splited_number.pop()
        elif "-" not in document_number and len(document_number) == 12:
            point_of_sale = document_number[:4]
            invoice_number = document_number[-8:]
        invoice_number = invoice_number and re.sub("[^0-9]", "", invoice_number)
        point_of_sale = point_of_sale and re.sub("[^0-9]", "", point_of_sale)
        if not invoice_number or not point_of_sale:
            raise ValidationError(_(
                'No pudimos obtener el número de factura y de punto de venta para %s %s. Verifique que tiene un número '
                'cargado similar a "00001-00000001"') % (document_type_code, document_number))
        return {
                'invoice_number': int(invoice_number),
                'point_of_sale': int(point_of_sale),
            }

    @api.constrains('ref', 'move_type', 'partner_id', 'journal_id', 'invoice_date')
    def _check_duplicate_supplier_reference(self):
        """ We make reference only unique if you are not using documents.
        Documents already guarantee to not encode twice same vendor bill """
        return super(
            AccountMove, self.filtered(lambda x: not x.l10n_latam_use_documents))._check_duplicate_supplier_reference()

    def _get_name_invoice_report(self, report_xml_id):
        """Use always argentinian like report (regardless use documents)"""
        self.ensure_one()
        if self.company_id.country_id.code == 'AR':
            custom_report = {
                'account.report_invoice_document': 'l10n_ar.report_invoice_document',
            }
            return custom_report.get(report_xml_id) or report_xml_id
        return super()._get_name_invoice_report(report_xml_id)

    def _get_l10n_latam_documents_domain(self):
        self.ensure_one()
        # TODO: add prefix "_l10n_ar" to method use_specific_document_types
        if self.company_id.country_id == self.env.ref('base.ar') and self.journal_id.use_specific_document_types():
            return [
                ('id', 'in', self.journal_id.l10n_ar_document_type_ids.ids),
                '|', ('code', 'in', self._get_l10n_ar_codes_used_for_inv_and_ref()),
                ('internal_type', 'in', ['credit_note'] if self.move_type in ['out_refund', 'in_refund'] else ['invoice', 'debit_note']),
            ]
        return super()._get_l10n_latam_documents_domain()

    def _post(self, soft=True):
        """ recompute debit/credit sending force_rate on context """
        other_curr_ar_invoices = self.filtered(
            lambda x: x.is_invoice() and
            x.company_id.country_id == self.env.ref('base.ar') and x.currency_id != x.company_id.currency_id)
        # llamamos a todos los casos de otra moneda y no solo a los que tienen "l10n_ar_currency_rate" porque odoo
        # tiene una suerte de bug donde solo recomputa los debitos/creditos en ciertas condiciones, pero puede
        # ser que esas condiciones no se cumplan y la cotizacion haya cambiado (por ejemplo la factura tiene fecha y
        # luego se cambia la cotizacion, al validar no se recomputa). Si odoo recomputase en todos los casos seria
        # solo necesario iterar los elementos con l10n_ar_currency_rate y hacer solo el llamado a super
        for rec in other_curr_ar_invoices:
            # si no tiene fecha en realidad en llamando a super ya se recomputa con el llamado a _onchange_invoice_date
            # también se recomputa con algo de lock dates llamando a _onchange_invoice_date, pero por si no se dan
            # esas condiciones o si odoo las cambia, llamamos al onchange_currency por las dudas
            rec.with_context(
                check_move_validity=False, force_rate=rec.l10n_ar_currency_rate)._onchange_currency()

            # tambien tenemos que pasar force_rate aca por las dudas de que super entre en onchange_currency en los
            # mismos casos mencionados recien
            res = super(AccountMove, rec.with_context(force_rate=rec.l10n_ar_currency_rate))._post(soft=soft)
        res = super(AccountMove, self - other_curr_ar_invoices)._post(soft=soft)
        return res
