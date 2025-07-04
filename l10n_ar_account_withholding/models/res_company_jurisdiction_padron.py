from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError
from io import BytesIO
import zipfile
from datetime import datetime
import tempfile
import os
import re
import logging
import base64
_logger = logging.getLogger(__name__)
import shutil

class ResCompanyJurisdictionPadron(models.Model):
    _name = "res.company.jurisdiction.padron"
    _description = "res.company.jurisdiction.padron"

    company_id = fields.Many2one(
        "res.company",
        required=True,
        default=lambda self: self.env.company,
    )
    jurisdiction_id = fields.Many2one(
        "account.account.tag",
        domain="[('applicability', '=', 'taxes'),('jurisdiction_code', '!=', False)]",
        required=True,
    )

    file_padron = fields.Binary(
        "File",
        required=True,
    )
    l10n_ar_padron_from_date = fields.Date(
        "From Date",
    )
    l10n_ar_padron_to_date = fields.Date(
        "To Date",
    )

    log_content = fields.Text(readonly=True)
    log_no_process = fields.Text(readonly=True)
    log_process = fields.Text(readonly=True)

    def reescribirAlicuotaARBA(self, contact=False):
        for rec in self:
            if not rec.file_padron:
                raise ValidationError('No se encuentra subido el archivo del padron')

            files_lines_dict, temp_dir = rec.open_file(rec)
            try:
                filtered_lines_dict = rec.find_contacts_in_line(files_lines_dict, contact)
                for file_name, find_lines in filtered_lines_dict.items():
                    for line in find_lines:
                        line = line.replace('\n', '')
                        try:
                            split_line = line.split(';')
                            tipo = split_line[0]  # "R" o "P"
                            from_date = datetime.strptime(split_line[2], '%d%m%Y').date()
                            to_date = datetime.strptime(split_line[3], '%d%m%Y').date()
                            cuit = split_line[4]
                            alicuot = float(split_line[8].replace(',', '.'))

                            if not self.l10n_ar_padron_from_date:
                                self.l10n_ar_padron_from_date = from_date
                            if not self.l10n_ar_padron_to_date:
                                self.l10n_ar_padron_to_date = to_date
                        except Exception as e:
                            rec.log_no_process += f'No se puede procesar la linea: {line}\n'
                            print(f'No se obtener los valores de la linea: {line}')
                            print(e)
                            continue

                        contact = self.env['res.partner'].search([('vat', '=', cuit)], limit=1)
                        rec.log_content += f'{line}\n'
                        if contact:
                            find = False
                            if contact.arba_alicuot_ids:
                                for ali in contact.arba_alicuot_ids:
                                    if ali.tag_id.id == rec.jurisdiction_id.id:
                                        if ali.from_date == from_date:
                                            find = True
                                            update_vals = {
                                                'tag_id': rec.jurisdiction_id.id,
                                                'from_date': from_date,
                                                'to_date': to_date,
                                                'date_last_update': datetime.now(),
                                            }
                                            if tipo == 'R':
                                                update_vals['alicuota_retencion'] = alicuot
                                            elif tipo == 'P':
                                                update_vals['alicuota_percepcion'] = alicuot
                                            ali.sudo().update(update_vals)
                                            rec.log_process += f'Linea procesada: {line}\n'
                            if not find:
                                new_line = {
                                    'tag_id': rec.jurisdiction_id.id,
                                    'from_date': from_date,
                                    'to_date': to_date,
                                    'withholding_amount_type': 'untaxed_amount',
                                }
                                if tipo == 'R':
                                    new_line['alicuota_retencion'] = alicuot
                                elif tipo == 'P':
                                    new_line['alicuota_percepcion'] = alicuot

                                try:
                                    contact.sudo().write({'arba_alicuot_ids': [(0, 0, new_line)]})
                                    rec.log_process += f'Linea procesada: {line}\n'
                                except Exception as e:
                                    rec.log_no_process += f'No se puede procesar la linea: {line}\n'
                                    print(e)
            finally:
                if temp_dir and os.path.exists(temp_dir):
                    shutil.rmtree(temp_dir)
                    os.remove(temp_dir)

    def descompress_file(self, file_padron):
        ruta_extraccion = tempfile.mkdtemp()  # crea carpeta temporal única
        file = base64.decodebytes(file_padron)

        with tempfile.NamedTemporaryFile(delete=False, suffix='.zip') as temp_zip:
            temp_zip.write(file)
            temp_zip_path = temp_zip.name

        try:
            with zipfile.ZipFile(temp_zip_path, 'r') as zip_file:
                zip_file.extractall(path=ruta_extraccion)
                archivos_txt = []
                for f in zip_file.namelist():
                    if f.lower().endswith('.txt'):
                        full_path = os.path.join(ruta_extraccion, f)
                        if os.path.isfile(full_path):  # seguridad extra
                            archivos_txt.append(full_path)
                return archivos_txt, ruta_extraccion
        except zipfile.BadZipFile:
            raise ValidationError("El archivo subido no es un ZIP válido.")



    def open_file(self, rec):
        rec.log_content = ''
        rec.log_no_process = ''
        rec.log_process = ''

        try:
            # Si es .txt puro, lo decodifica
            txt_content = base64.b64decode(rec.file_padron).decode('utf-8')
            lines = txt_content.replace('\r', '').split('\n')
            return {'file.txt': list(filter(None, lines))}
        except UnicodeDecodeError:
            # Si es ZIP
            file_paths, temp_dir = self.descompress_file(rec.file_padron)
            if not file_paths:
                raise ValidationError('No se encontraron archivos .txt dentro del ZIP.')

            all_lines = {}
            for path in file_paths:
                file_name = os.path.basename(path)
                try:
                    with open(path, 'r', encoding='utf-8', errors='ignore') as fp:
                        lines = [line.strip() for line in fp if line.strip()]
                        all_lines[file_name] = lines
                except Exception as e:
                    rec.log_no_process += f"No se pudo leer el archivo: {file_name}\n"
            return all_lines, temp_dir




    def find_contacts_in_line(self, files_lines_dict, contact=False):
        result = {}
        if not contact:
            contacts = self.env['res.partner'].search([('vat', '!=', False)])
        else:
            contacts = contact

        cuit_array = [c.vat for c in contacts]

        for file_name, lines in files_lines_dict.items():
            find_lines = []
            for line in lines:
                parts = line.split(';')
                if len(parts) > 4:
                    cuit = parts[4]
                    if cuit in cuit_array:
                        find_lines.append(line)
            if find_lines:
                result[file_name] = find_lines
        return result


    @api.constrains('jurisdiction_id')
    def check_jurisdiction_id(self):
        arba_tag = self.env.ref('l10n_ar_ux.tag_tax_jurisdiccion_902')
        for rec in self:
            if rec.jurisdiction_id != arba_tag:
                raise ValidationError("El padron para (%s) no está implementado." % rec.jurisdiction_id.name)

    @api.depends('company_id', 'jurisdiction_id')
    def name_get(self):
        res = []
        for padron in self:
            name = "%s: %s" % (padron.company_id.name,
                               padron.jurisdiction_id.name)
            res += [(padron.id, name)]
        return res

    def find_aliquot(self, path, cuit):
        """We try to find aliqut and number for a partner given
        """
        with open(path, "r") as fp:
            aliq = False
            nro = False
            for line in fp.readlines():
                values = line.split(";")
                if values[4] == cuit:
                    aliq = values[8]
                    nro = values[3]
                    break
            return nro, aliq

    def find_file(self, rootdir, type_code):
        res = False
        if not self.l10n_ar_padron_from_date:
            return res
        date = str(self.l10n_ar_padron_from_date.month) + \
            str(self.l10n_ar_padron_from_date.year)
        pattern = "%s.{1}|.TXT\Z" % type_code + date
        for subdir, dirs, files in os.walk(rootdir):
            for f in files:
                if re.search(pattern, f):
                    res = f
                    break
        return res

    def _get_aliquit(self, partner):
        padron_types = ["Per", "Ret"]
        nro = False
        aliquot_ret = 0.0
        aliquot_per = 0.0
        for padron_type in padron_types:
            path_file = self.find_file("/tmp/", padron_type)
            if not path_file:
                self.descompress_file(self.file_padron)
                path_file = self.find_file("/tmp/", padron_type)
            try:
                nro, aliquot = self.find_aliquot("/tmp/" + path_file, partner.vat)
            except:
                _logger.info(f"-- 114 Problema en el path_file = {path_file} ")
                nro, aliquot = 0,0

            if padron_type == "Per":
                aliquot_per = aliquot and aliquot.replace(",", ".")
            else:
                aliquot_ret = aliquot and aliquot.replace(",", ".")
        return nro, aliquot_ret, aliquot_per
