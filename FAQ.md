# Frequently Asked Questions

## Why choose the LGPL over any other license?

There are a few primary reasons over this but here is a quick overview why:

* We want to allow anyone using Open Cursor SDK's in open-source (copyleft or permissive) or proprietary applications (because the official Cursor SDK only allows by default usage in permissively licensed projects where linking a non-free library is allowed), BUT
    * We don't want the Cursor developers taking advantage of our SDK's unless they give back something in return.
    * They can still make their SDK's for other languages and license it in their own ways, however anyone wanting to use the Cursor API's without relying on hacky solutions and inefficient binding ways (e.g. in their [official Python SDK](https://forum.cursor.com/t/introducing-the-cursor-python-sdk/161367) where it's just wrapping a TypeScript SDK + a Node.js runtime) can use our SDK's.
* We want to library to be for the foreseeable future be free software to let anyone develop their AI related applications with a Cursor subscription without giving up their rights by using a non-free SDK.

## Isn't this breaking the Cursor TOS by doing this?

We don't want to be political, but if Cursor can get away with [training their models](https://cursor.com/changelog/composer-2-5) on stolen data which involves breaking licenses [due to the way that AI training works](https://codeberg.org/ethical-foss/open-slopware/src/branch/main/why_not_llms.md#stolen-training-data) then why not fight back (aka fighting fire with fire). 


